# Customisation Guide

This scraper is intentionally generic. Most ecommerce platforms expose useful product data through JSON-LD, Open Graph tags, and visible HTML, but every theme is a little different.

## Change the Product URL Filter

The default filter is `/product/`.

```bash
python scrape_sitemap_products.py \
  --sitemap "https://example.com/sitemap.xml" \
  --product-url-filter "/products/"
```

You can also pass a regex-like pattern:

```bash
python scrape_sitemap_products.py \
  --sitemap "https://example.com/sitemap.xml" \
  --product-url-filter "/(product|products)/"
```

Use a filter that matches the product URL pattern for your own site.

## Add Site-Specific Selectors

Start with `extract_visible_product_data()` in `scrape_sitemap_products.py`.

That function contains fallback selectors for:

- product name
- price
- SKU
- description

For example, if your theme stores the price in `.product-price-current`, add it near the other price selectors:

```python
price = first_non_empty(
    soup.select_one("[itemprop='price']"),
    soup.select_one("[data-product-price]"),
    soup.select_one(".product-price-current"),
    soup.select_one(".product-price"),
    soup.select_one(".price"),
)
```

For image-heavy themes, look at `extract_page_image_urls()`. Add custom attributes or gallery selectors there if your theme stores product images in custom components.

## Change Image Filtering

Image filtering happens in `filter_image_urls()` and `is_probably_tracking_image()`.

Common edits:

- Add or remove keywords in `SKIP_IMAGE_KEYWORDS`
- Allow another file extension in `IMAGE_EXTENSIONS`
- Adjust the tiny-image checks for `width`, `height`, and byte size

Keep the filtering conservative. It is better to skip obvious icons and tracking pixels than to fill product folders with theme assets.

## Export Shopify-Ready CSV Later

This repository exports a neutral product dataset. A Shopify-ready export can be added as a separate transformation step.

Recommended approach:

1. Keep `products.csv` and `products.json` as the raw scrape output.
2. Add a new script such as `export_shopify_csv.py`.
3. Map fields like title, body HTML, vendor, tags, variant SKU, variant price, and image URLs.
4. Validate the result against Shopify's current CSV import format before importing.

This keeps scraping separate from platform-specific migration logic.

## Add Cloudinary Uploading Later

Cloudinary is not the scraper. It can be added later as an image hosting, optimisation, CDN, and transformation layer after image URLs are discovered or local files are downloaded.

Recommended flow:

```text
scrape products -> download local files -> upload to Cloudinary -> write hosted image URLs to a new export
```

Implementation options:

- Upload each file in `downloaded_images`
- Use the product slug as the Cloudinary folder
- Store Cloudinary public IDs and secure URLs in a new JSON or CSV export
- Keep local scraping functional even when Cloudinary credentials are not configured
