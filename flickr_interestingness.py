#!/usr/bin/env python3
"""Create a Flickr photoset of your most interesting photos."""

import argparse
import os
import sys
import time
from datetime import datetime

import flickrapi
from dotenv import load_dotenv


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create a Flickr photoset from your most interesting photos."
    )
    parser.add_argument("--api-key", help="Flickr API key (overrides env/dotenv)")
    parser.add_argument("--api-secret", help="Flickr API secret (overrides env/dotenv)")
    parser.add_argument(
        "--title",
        default="Top 1000 Most Interesting",
        help="Photoset title (default: 'Top 1000 Most Interesting')",
    )
    parser.add_argument(
        "--description",
        default="Auto-generated set of my most interesting photos.",
        help="Photoset description",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1000,
        help="Number of photos to include (default: 1000)",
    )
    parser.add_argument(
        "--photoset-id",
        help="Update an existing photoset by ID",
    )
    parser.add_argument(
        "--photoset-name",
        help="Update an existing photoset by name (looked up from your photosets)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List photo IDs without creating the photoset",
    )
    return parser.parse_args()


def resolve_credentials(args):
    """Resolve API credentials with priority: CLI args > env vars > .env file."""
    load_dotenv()
    api_key = args.api_key or os.environ.get("FLICKR_API_KEY")
    api_secret = args.api_secret or os.environ.get("FLICKR_API_SECRET")
    if not api_key or not api_secret:
        print(
            "Error: Flickr API key and secret are required.\n"
            "Provide them via --api-key/--api-secret, environment variables, or a .env file.\n"
            "Get your credentials at https://www.flickr.com/services/apps/create/",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key, api_secret


def authenticate(api_key, api_secret):
    """Authenticate with Flickr via OAuth and return (flickr, user_nsid)."""
    flickr = flickrapi.FlickrAPI(api_key, api_secret, format="parsed-json")
    if not flickr.token_valid(perms="write"):
        flickr.authenticate_via_browser(perms="write")
    nsid = flickr.token_cache.token.user_nsid
    print(f"Authenticated as user: {nsid}")
    return flickr, nsid


def api_call_with_retry(func, max_retries=3, **kwargs):
    """Call a Flickr API method with exponential backoff on transient errors."""
    for attempt in range(max_retries):
        try:
            return func(**kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            print(f"  Transient error: {e}. Retrying in {wait}s...")
            time.sleep(wait)


def resolve_photoset_name(flickr, nsid, name):
    """Look up a photoset ID by name from the user's photosets."""
    page = 1
    while True:
        resp = api_call_with_retry(
            flickr.photosets.getList,
            user_id=nsid,
            per_page=500,
            page=page,
        )
        photosets = resp["photosets"]["photoset"]
        for ps in photosets:
            if ps["title"]["_content"] == name:
                print(f"Found photoset '{name}' with ID: {ps['id']}")
                return ps["id"]
        if page >= int(resp["photosets"]["pages"]):
            break
        page += 1
    print(f"Error: No photoset found with name '{name}'.", file=sys.stderr)
    sys.exit(1)


def fetch_interesting_photos(flickr, nsid, count):
    """Fetch the user's most interesting photos via paginated search."""
    photo_ids = []
    per_page = 500
    total_pages = (count + per_page - 1) // per_page

    for page in range(1, total_pages + 1):
        print(f"Fetching page {page}/{total_pages}...")
        resp = api_call_with_retry(
            flickr.photos.search,
            user_id=nsid,
            sort="interestingness-desc",
            per_page=per_page,
            page=page,
        )
        photos = resp["photos"]["photo"]
        if not photos:
            break
        photo_ids.extend(p["id"] for p in photos)
        if int(resp["photos"]["pages"]) <= page:
            break

    photo_ids = photo_ids[:count]
    print(f"Found {len(photo_ids)} interesting photos.")
    return photo_ids


def create_photoset(flickr, title, description, photo_ids):
    """Create a photoset and add all photos to it."""
    print(f"Creating photoset '{title}' with {len(photo_ids)} photos...")
    resp = api_call_with_retry(
        flickr.photosets.create,
        title=title,
        description=description,
        primary_photo_id=photo_ids[0],
    )
    photoset_id = resp["photoset"]["id"]
    print(f"Photoset created with ID: {photoset_id}")

    # Fast path: try editPhotos with all IDs at once
    try:
        print("Attempting bulk add via editPhotos...")
        api_call_with_retry(
            flickr.photosets.editPhotos,
            photoset_id=photoset_id,
            primary_photo_id=photo_ids[0],
            photo_ids=",".join(photo_ids),
        )
        print("All photos added successfully via editPhotos.")
    except Exception as e:
        print(f"editPhotos failed ({e}), falling back to addPhoto loop...")
        add_photos_individually(flickr, photoset_id, photo_ids)

    return photoset_id


def update_photoset(flickr, photoset_id, title, description, photo_ids):
    """Update an existing photoset with new photos and metadata."""
    print(f"Updating photoset '{photoset_id}' with {len(photo_ids)} photos...")

    # Append timestamp to description
    timestamp = datetime.now().astimezone().strftime("%B %d, %Y at %I:%M %p %Z")
    description = f"{description}\n\nLast updated: {timestamp}"

    # Update metadata
    print("Updating photoset title and description...")
    api_call_with_retry(
        flickr.photosets.editMeta,
        photoset_id=photoset_id,
        title=title,
        description=description,
    )

    # Replace all photos
    try:
        print("Replacing photos via editPhotos...")
        api_call_with_retry(
            flickr.photosets.editPhotos,
            photoset_id=photoset_id,
            primary_photo_id=photo_ids[0],
            photo_ids=",".join(photo_ids),
        )
        print("All photos replaced successfully via editPhotos.")
    except Exception as e:
        print(f"editPhotos failed ({e}), falling back to addPhoto loop...")
        add_photos_individually(flickr, photoset_id, photo_ids)

    return photoset_id


def add_photos_individually(flickr, photoset_id, photo_ids):
    """Fallback: add photos one by one with progress reporting."""
    # The first photo is already in the set (it's the primary photo)
    remaining = photo_ids[1:]
    added = 0
    failed = 0
    failures = []

    for i, photo_id in enumerate(remaining, start=1):
        try:
            api_call_with_retry(
                flickr.photosets.addPhoto,
                photoset_id=photoset_id,
                photo_id=photo_id,
            )
            added += 1
        except Exception as e:
            failed += 1
            failures.append((photo_id, str(e)))

        if i % 50 == 0 or i == len(remaining):
            print(f"  Progress: {i}/{len(remaining)} (added: {added}, failed: {failed})")

        time.sleep(0.1)  # 100ms delay between calls

    if failures:
        print(f"\nFailed to add {failed} photo(s):")
        for photo_id, err in failures:
            print(f"  Photo {photo_id}: {err}")
    else:
        print("All photos added successfully via addPhoto loop.")


def main():
    args = parse_args()
    api_key, api_secret = resolve_credentials(args)
    flickr, nsid = authenticate(api_key, api_secret)

    photo_ids = fetch_interesting_photos(flickr, nsid, args.count)

    if not photo_ids:
        print("No photos found. Nothing to do.")
        sys.exit(0)

    if args.dry_run:
        print(f"\n[DRY RUN] Would create photoset '{args.title}' with {len(photo_ids)} photos:")
        for pid in photo_ids:
            print(f"  {pid}")
        sys.exit(0)

    target_photoset_id = args.photoset_id
    if not target_photoset_id and args.photoset_name:
        target_photoset_id = resolve_photoset_name(flickr, nsid, args.photoset_name)

    if target_photoset_id:
        photoset_id = update_photoset(
            flickr, target_photoset_id, args.title, args.description, photo_ids
        )
    else:
        photoset_id = create_photoset(flickr, args.title, args.description, photo_ids)

    owner = nsid.replace("@", "%40")
    url = f"https://www.flickr.com/photos/{owner}/sets/{photoset_id}"
    print(f"\nDone! View your photoset at:\n  {url}")


if __name__ == "__main__":
    main()
