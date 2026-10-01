"""API version used for Instagram Login resource requests.

Token exchange and refresh endpoints are intentionally unversioned, per Meta's
Business Login guide. Resource paths follow the Instagram Login get-started guide.
"""

GRAPH_ROOT = 'https://graph.instagram.com'
GRAPH_VERSION = 'v26.0'


def resource_path(path):
    return f'/{GRAPH_VERSION}/{path.lstrip("/")}'
