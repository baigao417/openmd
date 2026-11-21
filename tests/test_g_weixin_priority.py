from openmd.routing.loader import default_backend_chains


def test_public_weixin_articles_try_local_http_before_browser():
    chain = default_backend_chains(effective=False)["weixin"]
    assert chain.index("weixin.article.local") < chain.index("opencli.weixin.download")

