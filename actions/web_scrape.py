"""Selenium web scraping module."""
from __future__ import annotations

import logging
import asyncio
from pathlib import Path
from sys import platform

from bs4 import BeautifulSoup
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.safari.options import Options as SafariOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from fastapi import WebSocket
from langchain.document_loaders import PyMuPDFLoader
from langchain.retrievers import ArxivRetriever

import processing.text as summary

from config import Config
from processing.html import extract_hyperlinks, format_hyperlinks

from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor()

FILE_DIR = Path(__file__).parent.parent
CFG = Config()

# 非同期でウェブサイトを閲覧し、ユーザーに答えとリンクを返す関数
async def async_browse(url: str, question: str, websocket: WebSocket) -> str:
    """ウェブサイトを閲覧し、ユーザーに答えとリンクを返す

    引数
        url (str): 閲覧するウェブサイトのURL
        question (str): ユーザーからの質問
        websocket (WebSocketManager): ウェブソケットマネージャ

    戻り値
        str: 答えとユーザーへのリンク
    """
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=8)

    print(f"{question}で {url}をスクレイピング中")
    await websocket.send_json(
        {
            "type": "logs",
            "output": f"🔎  {url} をブラウジングしています。 質問内容: {question}...",
        }
    )

    try:
        driver, text = await loop.run_in_executor(
            executor, scrape_text_with_selenium, url
        )
        await loop.run_in_executor(executor, add_header, driver)
        summary_text = await loop.run_in_executor(
            executor, summary.summarize_text, url, text, question, driver
        )

        await websocket.send_json(
            {
                "type": "logs",
                "output": f"📝 URLから収集した情報 {url}: {summary_text}",
            }
        )

        return f"Information gathered from url {url}: {summary_text}"
    except Exception as e:
        print(f"An error occurred while processing the url {url}: {e}")
        return f"Error processing the url {url}: {e}"

# Seleniumを使用してスクレイピングする関数
def browse_website(url: str, question: str) -> tuple[str, WebDriver]:
    """Seleniumを使用してウェブサイトからテキストをスクレイピングします。
    Args:
        url (str): スクレイピングするウェブサイトのURL
    Returns:
        Tuple[WebDriver, str]: WebDriverとウェブサイトからスクレイピングしたテキスト
    """

    if not url:
        return "URLが指定されていないため、ウェブサイト閲覧のリクエストをキャンセルしました。", None

    driver, text = scrape_text_with_selenium(url)
    add_header(driver)
    summary_text = summary.summarize_text(url, text, question, driver)

    links = scrape_links_with_selenium(driver, url)

    # Limit links to 5
    if len(links) > 5:
        links = links[:5]

    # write_to_file('research-{0}.txt'.format(url), summary_text + "\nSource Links: {0}\n\n".format(links))

    close_browser(driver)
    return f"Answer gathered from website: {summary_text} \n \n Links: {links}", driver


def scrape_text_with_selenium(url: str) -> tuple[WebDriver, str]:
    """seleniumを使ってウェブサイトからテキストをスクレイピングする

    引数
        url (str): スクレイピングするウェブサイトの url

    戻り値
        Tuple[WebDriver, str]: ウェブドライバとウェブサイトからスクレイピングされたテキスト
    """
    logging.getLogger("selenium").setLevel(logging.CRITICAL)

    options_available = {
        "chrome": ChromeOptions,
        "safari": SafariOptions,
        "firefox": FirefoxOptions,
    }

    options = options_available[CFG.selenium_web_browser]()
    options.add_argument(f"user-agent={CFG.user_agent}")
    options.add_argument("--headless")
    options.add_argument("--enable-javascript")

    if CFG.selenium_web_browser == "firefox":
        service = Service(executable_path=GeckoDriverManager().install())
        driver = webdriver.Firefox(service=service, options=options)
    elif CFG.selenium_web_browser == "safari":
        # Requires a bit more setup on the users end
        # See https://developer.apple.com/documentation/webkit/testing_with_webdriver_in_safari
        driver = webdriver.Safari(options=options)
    else:
        if platform == "linux" or platform == "linux2":
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--remote-debugging-port=9222")
        options.add_argument("--no-sandbox")
        options.add_experimental_option("prefs", {"download_restrictions": 3})
        driver = webdriver.Chrome(options=options)

    print(f"scraping url {url}...")
    driver.get(url)

    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )

    # check if url is a pdf or arxiv link
    if url.endswith(".pdf"):
        text = scrape_pdf_with_pymupdf(url)
    elif "arxiv" in url:
        # parse the document number from the url
        doc_num = url.split("/")[-1]
        text = scrape_pdf_with_arxiv(doc_num)
    else:
        # Get the HTML content directly from the browser's DOM
        page_source = driver.execute_script("return document.body.outerHTML;")
        soup = BeautifulSoup(page_source, "html.parser")

        for script in soup(["script", "style"]):
            script.extract()

        # text = soup.get_text()
        text = get_text(soup)

    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    text = "\n".join(chunk for chunk in chunks if chunk)
    return driver, text


def get_text(soup):
    """スープからテキストを取得する

    引数
        スープ(BeautifulSoup): テキストを取得するスープ

    戻り値
        str: スープからのテキスト
    """
    text = ""
    tags = ["h1", "h2", "h3", "h4", "h5", "p"]
    for element in soup.find_all(tags):  # Find all the <p> elements
        text += element.text + "\n\n"
    return text


def scrape_links_with_selenium(driver: WebDriver, url: str) -> list[str]:
    """セレニウムを使ってウェブサイトからリンクをスクレイピングする

    引数
        driver (WebDriver): リンクをスクレイピングするために使用するウェブドライバ

    戻り値
        リスト[str]: ウェブサイトからスクレイピングされたリンク
    """
    page_source = driver.page_source
    soup = BeautifulSoup(page_source, "html.parser")

    for script in soup(["script", "style"]):
        script.extract()

    hyperlinks = extract_hyperlinks(soup, url)

    return format_hyperlinks(hyperlinks)


def close_browser(driver: WebDriver) -> None:
    """ブラウザを閉じる

    引数
        driver (WebDriver): ウェブドライバを閉じる

    戻り値
        なし
    """
    driver.quit()


def add_header(driver: WebDriver) -> None:
    """ウェブサイトにヘッダーを追加する

    引数
        driver (WebDriver): ヘッダーを追加するために使用するウェブドライバー

    戻り値
        なし
    """
    driver.execute_script(open(f"{FILE_DIR}/js/overlay.js", "r").read())


def scrape_pdf_with_pymupdf(url) -> str:
    """Scrape a pdf with pymupdf

    Args:
        url (str): The url of the pdf to scrape

    Returns:
        str: The text scraped from the pdf
    """
    loader = PyMuPDFLoader(url)
    doc = loader.load()
    return str(doc)


def scrape_pdf_with_arxiv(query) -> str:
    """Scrape a pdf with arxiv
    default document length of 70000 about ~15 pages or None for no limit

    Args:
        query (str): The query to search for

    Returns:
        str: The text scraped from the pdf
    """
    retriever = ArxivRetriever(load_max_docs=2, doc_content_chars_max=None)
    docs = retriever.get_relevant_documents(query=query)
    return docs[0].page_content