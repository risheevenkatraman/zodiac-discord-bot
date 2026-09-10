"""Normalize the single configured X account and its incoming post links."""
import re
import time
from urllib.parse import urlparse


class XMonitor:
    def __init__(self, username: str, bearer_token: str, database, deliver) -> None:
        self.username = username
        self.bearer_token = bearer_token
        self.database = database
        self.deliver = deliver
        self.user_id = None
        self.next_attempt = 0.0

    async def get(self, session, path: str, params=None):
        async with session.get(
            f"https://api.x.com/2/{path}", params=params,
            headers={"Authorization": f"Bearer {self.bearer_token}"},
        ) as response:
            if response.status in {401, 402, 403, 429}:
                delay = 3600 if response.status != 429 else 900
                if response.status == 429:
                    try:
                        delay = max(delay, float(response.headers.get("x-rate-limit-reset", "0")) - time.time())
                    except ValueError:
                        pass
                self.next_attempt = time.monotonic() + delay
                raise RuntimeError(f"X API returned HTTP {response.status}; check bearer token, API access/credits, or rate limits. Retrying after {int(delay)} seconds.")
            response.raise_for_status()
            payload = await response.json()
            if not isinstance(payload, dict) or payload.get("errors"):
                raise RuntimeError("X returned an incomplete response; retaining the previous checkpoint.")
            return payload

    async def poll(self, session) -> None:
        if time.monotonic() < self.next_attempt:
            return
        if self.user_id is None:
            payload = await self.get(session, f"users/by/username/{self.username}")
            self.user_id = payload.get("data", {}).get("id")
            if not isinstance(self.user_id, str) or not self.user_id.isdigit():
                self.user_id = None
                raise RuntimeError("X could not resolve the configured account.")
        checkpoint = await self.database.get_x_checkpoint(self.user_id)
        params = {"max_results": "5" if checkpoint is None else "100",
                  "exclude": "retweets,replies", "tweet.fields": "author_id,referenced_tweets"}
        if checkpoint and checkpoint != "0":
            params["since_id"] = checkpoint
        posts = {}
        for _ in range(32):
            payload = await self.get(session, f"users/{self.user_id}/tweets", params)
            for post in payload.get("data", []):
                post_id = post.get("id")
                if not isinstance(post_id, str) or not post_id.isdigit():
                    raise RuntimeError("X returned an invalid post ID.")
                posts[post_id] = post
            next_token = payload.get("meta", {}).get("next_token")
            if checkpoint is None or not next_token:
                break
            params["pagination_token"] = next_token
        else:
            raise RuntimeError("X pagination exceeded its limit; retaining the checkpoint.")
        newest = max(posts, key=int) if posts else checkpoint or "0"
        if checkpoint is None:
            # Start with future posts rather than flooding Discord with history.
            await self.database.set_x_checkpoint(self.user_id, newest)
            return
        for post_id in sorted(posts, key=int):
            post = posts[post_id]
            if int(post_id) <= int(checkpoint):
                continue
            if post.get("author_id") != self.user_id or any(
                reference.get("type") in {"retweeted", "replied_to"}
                for reference in post.get("referenced_tweets", [])
            ):
                continue
            if not await self.deliver(self.username, post_id):
                return  # Retry failed destinations; successful ones are deduplicated.
        if newest != checkpoint:
            await self.database.set_x_checkpoint(self.user_id, newest)


def account_username(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {
            "x.com", "www.x.com", "twitter.com", "www.twitter.com"
        }:
            raise ValueError("X_ACCOUNT must be an https://x.com/account profile URL or @username.")
        value = parsed.path.strip("/")
    value = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value):
        raise ValueError("X_ACCOUNT must contain one account username, not a post URL.")
    return value.casefold()


def post_identity(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {
        "x.com", "www.x.com", "twitter.com", "www.twitter.com"
    }:
        raise ValueError("Expected an X post URL.")
    match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/status/([0-9]{1,20})/?", parsed.path)
    if not match:
        raise ValueError("Expected an X post URL.")
    return match[1].casefold(), match[2]
