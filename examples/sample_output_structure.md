# Sample Output Structure

If you run the scraper with `--output scraped_products`, the output will look like this:

```text
scraped_products/
  products.csv
  products.json
  images/
    example-product/
      image_001_a1b2c3d4e5f6.webp
      image_002_b2c3d4e5f6a1.webp
    another-product/
      image_001_c3d4e5f6a1b2.jpg
```

## Notes

- `products.csv` is intended for quick inspection, spreadsheet work, and migration planning.
- `products.json` keeps list fields as arrays and also includes normalised JSON-LD data when found.
- Each product gets its own image folder based on a safe product slug.
- Image files include a short SHA-256 content hash so repeated downloads can be identified.

## Cloudinary Is Not the Scraper

Cloudinary is not the scraper. It can be added later as an image hosting, optimisation, CDN, and transformation layer after image URLs are discovered or local files are downloaded.

A typical future flow could be:

```text
sitemap -> product pages -> local image files -> Cloudinary upload -> hosted CDN URLs
```
