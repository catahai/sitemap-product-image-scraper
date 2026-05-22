from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import mimetypes
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

if sys.version_info < (3, 11):
    raise SystemExit("This scraper requires Python 3.11 or newer.")

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Page, Route, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_NAME = "Sitemap Product Image Scraper"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

CSV_FIELDS = [
    "product_url",
    "name",
    "price",
    "currency",
    "sku",
    "availability",
    "description",
    "image_count_found",
    "image_count_downloaded",
    "image_urls",
    "downloaded_images",
    "error",
]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
SKIP_IMAGE_KEYWORDS = {
    "favicon",
    "icon",
    "logo",
    "sprite",
    "placeholder",
    "transparent",
    "tracking",
    "pixel",
    "blank",
    "loader",
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ProductRecord:
    product_url: str
    name: str = ""
    price: str = ""
    currency: str = ""
    sku: str = ""
    availability: str = ""
    description: str = ""
    image_count_found: int = 0
    image_count_downloaded: int = 0
    image_urls: list[str] = field(default_factory=list)
    downloaded_images: list[str] = field(default_factory=list)
    json_ld: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class ImageDownload:
    url: str
    path: str
    digest: str


@dataclass(frozen=True)
class ScraperConfig:
    sitemap: str
    output: Path
    max_products: int
    headful: bool
    delay: float
    timeout: float
    product_url_filter: str


class RobotsCache:
    """Small robots.txt cache so the scraper does not knowingly ignore published rules."""

    def __init__(self, session: requests.Session, user_agent: str, timeout: float) -> None:
        self.session = session
        self.user_agent = user_agent
        self.timeout = timeout
        self._parsers: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parser = self._parser_for(url)
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    def _parser_for(self, url: str) -> RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._parsers:
            return self._parsers[origin]

        parser = RobotFileParser()
        try:
            response = self.session.get(urljoin(origin, "/robots.txt"), timeout=self.timeout)
            if response.status_code >= 400:
                self._parsers[origin] = None
                return None
            parser.parse(response.text.splitlines())
        except requests.RequestException:
            self._parsers[origin] = None
            return None

        self._parsers[origin] = parser
        return parser


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return number


def non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return number


def positive_float(value: str) -> float:
    number = non_negative_float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def parse_args() -> ScraperConfig:
    parser = argparse.ArgumentParser(
        description="Extract ecommerce product data and images from sitemap-listed product pages.",
        epilog=(
            "Ethical use: only run this on websites you own, administer, "
            "or have explicit permission to process."
        ),
    )
    parser.add_argument("--sitemap", required=True, help="URL of the sitemap.xml file.")
    parser.add_argument(
        "--output",
        default="output",
        help="Output directory for CSV, JSON, and images. Default: output",
    )
    parser.add_argument(
        "--max-products",
        type=non_negative_int,
        default=0,
        help="Maximum products to scrape. Use 0 for all products. Default: 0",
    )
    parser.add_argument("--headful", action="store_true", help="Run Playwright with a visible browser.")
    parser.add_argument(
        "--delay",
        type=non_negative_float,
        default=0.0,
        help="Delay between product pages in seconds. Default: 0",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=30.0,
        help="Page and network timeout in seconds. Default: 30",
    )
    parser.add_argument(
        "--product-url-filter",
        default="/product/",
        help="Substring or regex used to identify product URLs. Default: /product/",
    )
    args = parser.parse_args()

    parsed_sitemap = urlparse(args.sitemap)
    if parsed_sitemap.scheme not in {"http", "https"} or not parsed_sitemap.netloc:
        parser.error("--sitemap must be an absolute http(s) URL, for example https://example.com/sitemap.xml")

    return ScraperConfig(
        sitemap=args.sitemap,
        output=Path(args.output),
        max_products=args.max_products,
        headful=args.headful,
        delay=args.delay,
        timeout=args.timeout,
        product_url_filter=args.product_url_filter,
    )


# ---------------------------------------------------------------------------
# Text, URL, and sitemap helpers
# ---------------------------------------------------------------------------


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def slugify(value: str, fallback: str = "product", max_length: int = 90) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug[:max_length].strip("-") or fallback)[:max_length]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(clean_text(item) for item in value)
    if hasattr(value, "get_text"):
        text = value.get_text(" ", strip=True)
    else:
        text = str(value)
        if "<" in text and ">" in text:
            text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def looks_like_regex(pattern: str) -> bool:
    return any(char in pattern for char in "^$.*+?{}[]\\|()")


def matches_product_filter(url: str, product_filter: str) -> bool:
    if not product_filter:
        return True
    if looks_like_regex(product_filter):
        try:
            return re.search(product_filter, url) is not None
        except re.error:
            return product_filter in url
    return product_filter in url


def normalize_url(raw_url: str, base_url: str) -> str:
    full_url = urljoin(base_url, raw_url.strip())
    parsed = urlparse(full_url)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))


def fetch_text(session: requests.Session, url: str, timeout: float) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def extract_sitemap_locations(xml: str) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(xml, "xml")
    sitemap_urls = [tag.get_text(strip=True) for tag in soup.select("sitemap > loc")]
    page_urls = [tag.get_text(strip=True) for tag in soup.select("url > loc")]

    if not sitemap_urls and not page_urls:
        page_urls = [tag.get_text(strip=True) for tag in soup.find_all("loc")]

    return sitemap_urls, page_urls


def discover_product_urls(
    sitemap_url: str,
    session: requests.Session,
    robots: RobotsCache,
    timeout: float,
    product_url_filter: str,
) -> list[str]:
    pending = [sitemap_url]
    seen_sitemaps: set[str] = set()
    product_urls: set[str] = set()

    while pending:
        current_sitemap = pending.pop(0)
        if current_sitemap in seen_sitemaps:
            continue
        seen_sitemaps.add(current_sitemap)

        if not robots.allowed(current_sitemap):
            print(f"Skipping sitemap disallowed by robots.txt: {current_sitemap}")
            continue

        try:
            sitemap_xml = fetch_text(session, current_sitemap, timeout)
        except requests.RequestException as error:
            print(f"Sitemap fetch failed: {current_sitemap} | {error}")
            continue

        child_sitemaps, page_urls = extract_sitemap_locations(sitemap_xml)
        pending.extend(normalize_url(url, current_sitemap) for url in child_sitemaps)

        for page_url in page_urls:
            absolute_url = normalize_url(page_url, current_sitemap)
            if matches_product_filter(absolute_url, product_url_filter):
                product_urls.add(absolute_url)

    return sorted(product_urls)


# ---------------------------------------------------------------------------
# Product data extraction
# ---------------------------------------------------------------------------


def iter_json_ld_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, list):
            yield from iter_json_ld_nodes(graph)
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_ld_nodes(item)


def is_product_node(node: dict[str, Any]) -> bool:
    node_type = node.get("@type") or node.get("type")
    product_types = {"product", "schema:product"}
    if isinstance(node_type, list):
        return any(str(item).lower() in product_types for item in node_type)
    return str(node_type).lower() in product_types


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def extract_json_ld_images(value: Any) -> list[str]:
    image_urls: list[str] = []
    for item in as_list(value):
        if isinstance(item, str):
            image_urls.append(item)
        elif isinstance(item, dict):
            for key in ("url", "contentUrl", "thumbnailUrl"):
                url = clean_text(item.get(key))
                if url:
                    image_urls.append(url)
    return image_urls


def extract_offer_data(offers: Any) -> dict[str, str]:
    offer_items = as_list(offers)
    offer = next(
        (
            item
            for item in offer_items
            if isinstance(item, dict) and str(item.get("@type", "")).lower() in {"offer", "aggregateoffer"}
        ),
        next((item for item in offer_items if isinstance(item, dict)), {}),
    )
    if not isinstance(offer, dict):
        return {}

    price = offer.get("price") or offer.get("lowPrice") or offer.get("highPrice")
    availability = clean_text(offer.get("availability")).rsplit("/", maxsplit=1)[-1]
    return {
        "price": clean_text(price),
        "currency": clean_text(offer.get("priceCurrency")),
        "availability": availability,
    }


def extract_json_ld_product_data(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    product_data: dict[str, Any] = {"images": []}

    for script in soup.find_all("script", type="application/ld+json"):
        raw_json = script.string or script.get_text()
        if not raw_json:
            continue

        try:
            parsed_json = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        for node in iter_json_ld_nodes(parsed_json):
            if not is_product_node(node):
                continue

            product_data["name"] = first_non_empty(product_data.get("name"), node.get("name"))
            product_data["description"] = first_non_empty(
                product_data.get("description"),
                node.get("description"),
            )
            product_data["sku"] = first_non_empty(product_data.get("sku"), node.get("sku"), node.get("mpn"))
            product_data["images"].extend(extract_json_ld_images(node.get("image")))
            product_data.update({key: value for key, value in extract_offer_data(node.get("offers")).items() if value})

    product_data["images"] = dedupe_preserve_order(product_data["images"])
    return product_data


def meta_content(soup: BeautifulSoup, *selectors: str) -> str:
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            return clean_text(tag.get("content"))
    return ""


def extract_visible_product_data(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")

    # Customise these selectors for a specific website when generic JSON-LD/HTML
    # signals are not enough. See docs/customisation.md for examples.
    name = first_non_empty(
        meta_content(soup, "meta[property='og:title']", "meta[name='twitter:title']"),
        soup.select_one("[data-product-title]"),
        soup.select_one(".product-title"),
        soup.select_one(".product__title"),
        soup.select_one("h1"),
        soup.title,
    )
    price = first_non_empty(
        soup.select_one("[itemprop='price']"),
        soup.select_one("[data-product-price]"),
        soup.select_one(".product-price"),
        soup.select_one(".price"),
    )
    sku = first_non_empty(
        soup.select_one("[itemprop='sku']"),
        soup.select_one("[data-sku]"),
        soup.select_one(".sku"),
    )
    description = first_non_empty(
        meta_content(soup, "meta[property='og:description']", "meta[name='description']"),
        soup.select_one("[itemprop='description']"),
        soup.select_one(".product-description"),
        soup.select_one(".product__description"),
    )

    if not price:
        text = soup.get_text(" ", strip=True)
        price_match = re.search(r"(?:A?\$|USD|AUD|EUR|GBP)\s?\d[\d,.]*(?:\.\d{2})?", text)
        price = price_match.group(0) if price_match else ""

    return {"name": name, "price": price, "sku": sku, "description": description}


def extract_page_image_urls(page: Page, html: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    image_urls: list[str] = []

    for selector in (
        "meta[property='og:image']",
        "meta[property='og:image:secure_url']",
        "meta[name='twitter:image']",
        "meta[name='twitter:image:src']",
    ):
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            image_urls.append(str(tag["content"]))

    # This Playwright pass reads rendered image attributes after JavaScript has
    # executed. Add site-specific gallery selectors here if a store hides images
    # behind custom components.
    image_urls.extend(
        page.evaluate(
            """
            () => {
              const values = [];
              const push = value => {
                if (value && typeof value === 'string') values.push(value.trim());
              };
              const visible = element => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width >= 20 && rect.height >= 20 &&
                  style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || 1) > 0;
              };
              const fromSrcset = value => {
                if (!value) return;
                value.split(',').forEach(part => push(part.trim().split(/\\s+/)[0]));
              };

              document.querySelectorAll('img').forEach(img => {
                if (!visible(img)) return;
                push(img.currentSrc);
                push(img.src);
                push(img.getAttribute('data-src'));
                push(img.getAttribute('data-image'));
                push(img.getAttribute('data-original'));
                push(img.getAttribute('data-zoom-image'));
                fromSrcset(img.getAttribute('srcset'));
                fromSrcset(img.getAttribute('data-srcset'));
              });

              document.querySelectorAll('picture source').forEach(source => {
                const picture = source.closest('picture');
                if (picture && visible(picture)) fromSrcset(source.getAttribute('srcset'));
              });

              return values;
            }
            """
        )
    )

    return filter_image_urls(image_urls, page_url)


# ---------------------------------------------------------------------------
# Image filtering and downloads
# ---------------------------------------------------------------------------


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def parse_dimension(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def filter_image_urls(raw_urls: Iterable[str], base_url: str) -> list[str]:
    filtered: list[str] = []

    for raw_url in raw_urls:
        if not raw_url:
            continue
        raw_url = raw_url.strip()
        if raw_url.startswith(("data:", "blob:", "javascript:", "mailto:")):
            continue

        image_url = normalize_url(raw_url, base_url)
        parsed = urlparse(image_url)
        path = parsed.path.lower()
        filename = os.path.basename(path)
        extension = os.path.splitext(path)[1]

        if extension == ".svg":
            continue
        if extension and extension not in IMAGE_EXTENSIONS:
            continue
        if any(keyword in image_url.lower() for keyword in SKIP_IMAGE_KEYWORDS):
            continue

        query = dict(parse_qsl(parsed.query.lower()))
        width = parse_dimension(query.get("w") or query.get("width"))
        height = parse_dimension(query.get("h") or query.get("height"))
        if width is not None and width <= 2:
            continue
        if height is not None and height <= 2:
            continue
        if filename in {"1x1.gif", "pixel.gif", "spacer.gif"}:
            continue

        filtered.append(image_url)

    return dedupe_preserve_order(filtered)


def image_extension(url: str, content_type: str) -> str:
    parsed_extension = os.path.splitext(urlparse(url).path)[1].lower()
    if parsed_extension in IMAGE_EXTENSIONS:
        return ".jpg" if parsed_extension == ".jpeg" else parsed_extension

    media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    guessed = mimetypes.guess_extension(media_type) or ""
    if guessed == ".jpe":
        return ".jpg"
    if guessed in IMAGE_EXTENSIONS:
        return guessed
    return ".img"


def is_probably_tracking_image(content: bytes, content_type: str, url: str) -> bool:
    if len(content) <= 128:
        return True
    lowered = f"{content_type} {url}".lower()
    return "pixel" in lowered or "tracking" in lowered


def download_image(
    session: requests.Session,
    image_url: str,
    product_folder: Path,
    index: int,
    timeout: float,
    global_hashes: dict[str, str],
    product_hashes: set[str],
    robots: RobotsCache,
) -> ImageDownload | None:
    if not robots.allowed(image_url):
        print(f"Image skipped by robots.txt: {image_url}")
        return None

    try:
        response = session.get(image_url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"Image failed: {image_url} | {error}")
        return None

    content_type = response.headers.get("Content-Type", "").lower()
    content = response.content
    if "image" not in content_type or "svg" in content_type:
        return None
    if is_probably_tracking_image(content, content_type, image_url):
        return None

    digest = hashlib.sha256(content).hexdigest()
    if digest in product_hashes:
        return None
    product_hashes.add(digest)

    extension = image_extension(image_url, content_type)
    output_path = product_folder / f"image_{index:03d}_{digest[:12]}{extension}"

    existing_path = global_hashes.get(digest)
    if existing_path:
        try:
            if not output_path.exists():
                os.link(existing_path, output_path)
        except OSError:
            return ImageDownload(url=image_url, path=existing_path, digest=digest)
        return ImageDownload(url=image_url, path=str(output_path), digest=digest)

    output_path.write_bytes(content)
    global_hashes[digest] = str(output_path)
    return ImageDownload(url=image_url, path=str(output_path), digest=digest)


# ---------------------------------------------------------------------------
# Browser scraping and exports
# ---------------------------------------------------------------------------


def block_heavy_resources(route: Route) -> None:
    request = route.request
    if request.resource_type in {"font", "image", "media"}:
        route.abort()
    else:
        route.continue_()


def scroll_for_lazy_images(page: Page) -> None:
    try:
        page.evaluate(
            """
            async () => {
              const step = Math.max(600, Math.floor(window.innerHeight * 0.8));
              const limit = Math.min(document.body.scrollHeight, window.innerHeight * 4);
              for (let y = 0; y < limit; y += step) {
                window.scrollTo(0, y);
                await new Promise(resolve => setTimeout(resolve, 150));
              }
              window.scrollTo(0, 0);
            }
            """
        )
    except Exception:
        return


def scrape_product(
    context: BrowserContext,
    product_url: str,
    output_dir: Path,
    session: requests.Session,
    timeout: float,
    global_hashes: dict[str, str],
    robots: RobotsCache,
) -> ProductRecord:
    record = ProductRecord(product_url=product_url)

    if not robots.allowed(product_url):
        record.error = "Blocked by robots.txt"
        return record

    page = context.new_page()
    page.set_default_timeout(timeout * 1000)
    page.set_default_navigation_timeout(timeout * 1000)

    try:
        page.goto(product_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_load_state("networkidle", timeout=min(timeout * 1000, 10_000))
    except PlaywrightTimeoutError:
        print(f"Page reached timeout; extracting whatever loaded: {product_url}")
        pass
    except Exception as error:
        record.error = f"Page load failed: {error}"
        page.close()
        return record

    try:
        scroll_for_lazy_images(page)
        html = page.content()
        json_ld = extract_json_ld_product_data(html)
        visible_data = extract_visible_product_data(html)

        record.json_ld = {key: value for key, value in json_ld.items() if value}
        record.name = first_non_empty(json_ld.get("name"), visible_data.get("name"), "unknown product")
        record.price = first_non_empty(json_ld.get("price"), visible_data.get("price"))
        record.currency = first_non_empty(json_ld.get("currency"))
        record.sku = first_non_empty(json_ld.get("sku"), visible_data.get("sku"))
        record.availability = first_non_empty(json_ld.get("availability"))
        record.description = first_non_empty(json_ld.get("description"), visible_data.get("description"))

        image_urls = extract_page_image_urls(page, html, product_url)
        image_urls.extend(normalize_url(url, product_url) for url in json_ld.get("images", []))
        record.image_urls = filter_image_urls(image_urls, product_url)
        record.image_count_found = len(record.image_urls)

        fallback_slug = slugify(urlparse(product_url).path)
        product_folder = output_dir / "images" / slugify(record.name, fallback=fallback_slug)
        product_folder.mkdir(parents=True, exist_ok=True)

        product_hashes: set[str] = set()
        for index, image_url in enumerate(record.image_urls, start=1):
            download = download_image(
                session=session,
                image_url=image_url,
                product_folder=product_folder,
                index=index,
                timeout=timeout,
                global_hashes=global_hashes,
                product_hashes=product_hashes,
                robots=robots,
            )
            if download:
                record.downloaded_images.append(download.path)

        record.downloaded_images = dedupe_preserve_order(record.downloaded_images)
        record.image_count_downloaded = len(record.downloaded_images)
    except Exception as error:
        record.error = str(error)
    finally:
        page.close()

    return record


def save_outputs(records: list[ProductRecord], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "images").mkdir(parents=True, exist_ok=True)

    records_as_dicts = [asdict(record) for record in records]
    (output_dir / "products.json").write_text(
        json.dumps(records_as_dicts, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    with (output_dir / "products.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records_as_dicts:
            row = {field: record.get(field, "") for field in CSV_FIELDS}
            row["image_urls"] = " | ".join(record.get("image_urls", []))
            row["downloaded_images"] = " | ".join(record.get("downloaded_images", []))
            writer.writerow(row)


def main() -> int:
    config = parse_args()
    output_dir = config.output
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "images").mkdir(parents=True, exist_ok=True)

    session = make_session()
    robots = RobotsCache(session=session, user_agent=DEFAULT_USER_AGENT, timeout=config.timeout)

    print(f"{SCRIPT_NAME}")
    print(f"Reading sitemap: {config.sitemap}")
    product_urls = discover_product_urls(
        sitemap_url=config.sitemap,
        session=session,
        robots=robots,
        timeout=config.timeout,
        product_url_filter=config.product_url_filter,
    )
    print(f"Product URLs found: {len(product_urls)}")

    if config.max_products > 0:
        product_urls = product_urls[: config.max_products]
        print(f"Limiting scrape to first {len(product_urls)} product(s)")

    records: list[ProductRecord] = []
    global_hashes: dict[str, str] = {}

    if not product_urls:
        print("No product URLs matched the filter. Writing empty CSV and JSON files.")
        save_outputs(records, output_dir)
        print(f"CSV saved to: {output_dir / 'products.csv'}")
        print(f"JSON saved to: {output_dir / 'products.json'}")
        return 0

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=not config.headful)
        except Exception as error:
            print(f"Could not launch Chromium with Playwright: {error}")
            print("Try running: playwright install chromium")
            return 1

        context = browser.new_context(user_agent=DEFAULT_USER_AGENT, java_script_enabled=True)
        context.route("**/*", block_heavy_resources)

        for index, product_url in enumerate(product_urls, start=1):
            print(f"[{index}/{len(product_urls)}] Scraping: {product_url}")
            record = scrape_product(
                context=context,
                product_url=product_url,
                output_dir=output_dir,
                session=session,
                timeout=config.timeout,
                global_hashes=global_hashes,
                robots=robots,
            )
            records.append(record)

            status = "failed" if record.error else "done"
            print(
                f"{status}: {record.name or record.product_url} | "
                f"images {record.image_count_downloaded}/{record.image_count_found}"
            )

            if config.delay > 0 and index < len(product_urls):
                time.sleep(config.delay)

        context.close()
        browser.close()

    save_outputs(records, output_dir)

    total_downloaded = sum(record.image_count_downloaded for record in records)
    failures = sum(1 for record in records if record.error)
    print("Finished.")
    print(f"Products processed: {len(records)}")
    print(f"Failures: {failures}")
    print(f"Images downloaded: {total_downloaded}")
    print(f"CSV saved to: {output_dir / 'products.csv'}")
    print(f"JSON saved to: {output_dir / 'products.json'}")
    print(f"Images saved to: {output_dir / 'images'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
