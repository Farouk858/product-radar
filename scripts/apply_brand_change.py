#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path

BRANDS_FILE = Path("brands.json")

def load_brands():
    if not BRANDS_FILE.exists():
        return []
    return json.loads(BRANDS_FILE.read_text(encoding="utf-8"))

def save_brands(data):
    BRANDS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", choices=["add","remove"], required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--url", default="")
    args = ap.parse_args()

    name = args.name.strip()
    url  = args.url.strip()

    brands = load_brands()

    def idx_of(nm):
        for i, it in enumerate(brands):
            if it.get("name","").lower() == nm.lower():
                return i
        return -1

    if args.action == "add":
        if not url:
            print("URL required for add", file=sys.stderr)
        i = idx_of(name)
        if i >= 0:
            if url and brands[i].get("url") != url:
                brands[i]["url"] = url
        else:
            if not url:
                sys.exit(1)
            brands.append({"name": name, "url": url})

    elif args.action == "remove":
        i = idx_of(name)
        if i >= 0:
            brands.pop(i)
        else:
            print(f"{name} not found ~ nothing to remove", file=sys.stderr)

    brands = sorted(brands, key=lambda x: x.get("name","").lower())
    save_brands(brands)
    print("brands.json updated")

if __name__ == "__main__":
    main()

