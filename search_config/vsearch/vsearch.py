#!/usr/bin/env python3

import sys, re, logging
import requests
import argparse
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from config import HEADERS, logger

def main():
    parser = argparse.ArgumentParser(description="Process URLs based on type.")
    parser.add_argument('--type', required=True, help='The type of processing (e.g., moviesmod)')
    parser.add_argument('url', help='The URL to process')

    args = parser.parse_args()

    if args.type == 'moviesmod':
        from moviesmod import process_url
        result = process_url(args.url)
    elif args.type == 'bollyflix':
        from bollyflix import process_url
        result = process_url(args.url)
    elif args.type == 'toonworld4all':
        from toonworld4all import process_url
        result = process_url(args.url)
    else:
        logger.error(f"Unknown type: {args.type}")
        sys.exit(1)
        
    if result:
        print(f"URL: {result}")
    else:
        logger.error("Failed to process the URL")
        sys.exit(1)

if __name__ == "__main__":
    main()