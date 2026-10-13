import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request


def send(payload, attempts=6):
    body = json.dumps(payload).encode()
    for attempt in range(1, attempts + 1):
        stamp = str(int(time.time()))
        signature = hmac.new(os.environ["DEPLOY_TOKEN"].encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            f"{os.environ['DEPLOY_URL']}/deploy",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Graphite-Timestamp": stamp,
                "X-Graphite-Signature": signature,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=660) as response:
                return json.load(response)["output"]
        except urllib.error.HTTPError as error:
            raise SystemExit(f"error: {payload['name']}: {json.loads(error.read() or b'{}').get('error', error.reason)}") from error
        except (urllib.error.URLError, ConnectionError) as error:
            if attempt == attempts:
                raise SystemExit(f"error: could not reach the deploy agent: {error}") from error
            print(f"The deploy agent is not reachable yet ({error}); retrying ({attempt}/{attempts}).", file=sys.stderr)
            time.sleep(10)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "secrets":
        print("usage: agent.py secrets <name> < secrets.env", file=sys.stderr)
        sys.exit(1)
    print(send({"name": sys.argv[2], "action": "secrets", "secrets": sys.stdin.read()}))
