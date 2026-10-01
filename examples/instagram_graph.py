import httpx
import time
from instagram_api import GRAPH_ROOT, resource_path


class InstagramGraphError(RuntimeError):
    pass


class InstagramGraph:
    def __init__(self, token, user_id):
        self.user_id = str(user_id)
        self.http = httpx.AsyncClient(base_url=GRAPH_ROOT, timeout=30,
                                      headers={"Authorization": f"Bearer {token}"})
        self.token = token

    async def close(self):
        await self.http.aclose()

    async def refresh_if_needed(self, data, token_file):
        from instagram_store import write_json
        if not data.get('expires_at'):
            data['expires_at'] = token_file.stat().st_mtime + data.get('expires_in', 3600)
            write_json(token_file, data)
        if data.get('temporary_token'):
            if data['expires_at'] <= time.time():
                raise InstagramGraphError('Temporary Instagram token expired. Reconnect Instagram.')
            return
        if data['expires_at'] - time.time() > 7*86400:
            return
        response = await self.http.get('/refresh_access_token', params={
            'grant_type': 'ig_refresh_token', 'access_token': self.token})
        if response.is_error:
            raise InstagramGraphError('Token refresh failed. Reconnect Instagram.')
        result = response.json()
        data.update(result, expires_at=time.time()+result['expires_in'])
        write_json(token_file, data)
        self.token = result['access_token']
        self.http.headers['Authorization'] = 'Bearer '+self.token

    async def _get(self, path, **params):
        response = await self.http.get(resource_path(path), params=params)
        if response.is_error:
            error = response.json().get("error", {})
            raise InstagramGraphError(f"Instagram error {error.get('code', response.status_code)}: "
                                      f"{error.get('message', 'Request failed')}".replace(self.token, '[redacted]'))
        return response.json()

    async def pages(self, path, limit=100, **params):
        items, cursors = [], set()
        while len(items) < limit:
            response = await self._get(path, limit=min(50, limit-len(items)), **params)
            items.extend(response.get("data", []))
            paging = response.get("paging", {})
            after = paging.get("cursors", {}).get("after")
            if not paging.get("next") or not after or after in cursors:
                break
            cursors.add(after)
            params["after"] = after
        return items[:limit]

    async def profile(self):
        return await self._get("/me", fields="user_id,username")

    async def media(self, limit=3):
        return await self.pages(
            f"/{self.user_id}/media", limit=limit,
            fields="id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,comments_count",
        )

    async def comments(self, media_id, limit=100):
        return await self.pages(
            f"/{media_id}/comments", limit=limit,
            fields="id,text,username,timestamp,from",
        )

    async def replies(self, comment_id):
        return await self.pages(f"/{comment_id}/replies", fields="id,text,username,timestamp")

    async def children(self, media_id):
        return await self.pages(f"/{media_id}/children", fields="id,media_type,media_url")


if __name__ == "__main__":
    assert InstagramGraphError.__mro__[1] is RuntimeError
    print("Self-test passed")
