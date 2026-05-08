#!/usr/bin/env python3
"""
Script to fetch protobuf schemas from GitHub API and store in proto folder.
Fetches byova_common.proto and voicevirtualagent.proto.
"""

import requests
import base64
import os
import sys


def fetch_proto_file(api_url, filename, proto_folder):
    try:
        print(f"Fetching {filename}...")
        response = requests.get(api_url)
        response.raise_for_status()
        data = response.json()

        if "content" not in data:
            raise Exception(f"No 'content' field found in API response for {filename}")

        decoded_content = base64.b64decode(data["content"]).decode('utf-8')
        print(f"Successfully fetched {filename} ({len(decoded_content)} characters)")

        proto_file_path = os.path.join(proto_folder, filename)
        with open(proto_file_path, 'w', encoding='utf-8') as f:
            f.write(decoded_content)

        print(f"Saved to: {proto_file_path}")
        return True

    except requests.exceptions.RequestException as e:
        print(f"HTTP Error for {filename}: {e}")
        return False
    except Exception as e:
        print(f"Error for {filename}: {e}")
        return False


def fetch_and_decode_proto_schemas():
    proto_files = [
        {
            "url": "https://api.github.com/repos/webex/dataSourceSchemas/git/blobs/a22cf246a1e0a3302c48100aea8e2c5c09b66f0a",
            "filename": "byova_common.proto"
        },
        {
            "url": "https://api.github.com/repos/webex/dataSourceSchemas/git/blobs/2cdba566b9b45d029874ecf4f649630741777023",
            "filename": "voicevirtualagent.proto"
        },
        {
            "url": "https://api.github.com/repos/webex/dataSourceSchemas/git/blobs/0d56517d9a6bbe231000f19aafcbe755679efeb6",
            "filename": "conversationaudioforking.proto"
        },
        {
            "url": "https://api.github.com/repos/webex/dataSourceSchemas/git/blobs/68f44b18b760bc3e9296f017e78118e64d112758",
            "filename": "media_service_common.proto"
        }
    ]

    proto_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto")
    os.makedirs(proto_folder, exist_ok=True)
    print(f"Proto folder: {os.path.abspath(proto_folder)}")

    success_count = 0
    for proto_file in proto_files:
        if fetch_proto_file(proto_file["url"], proto_file["filename"], proto_folder):
            success_count += 1

    return success_count == len(proto_files)


def main():
    print("Proto Schema Fetcher")
    print("=" * 40)
    success = fetch_and_decode_proto_schemas()
    if success:
        print("\nAll proto schemas fetched successfully!")
    else:
        print("\nProto schema fetch failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
