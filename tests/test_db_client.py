import importlib

from motor.motor_asyncio import AsyncIOMotorClient


def _reload():
    import packages.db.client as dbclient
    importlib.reload(dbclient)
    return dbclient


def test_get_client_singleton():
    dbclient = _reload()
    c1 = dbclient.get_client()
    c2 = dbclient.get_client()
    assert c1 is c2
    assert isinstance(c1, AsyncIOMotorClient)


def test_get_client_env_uri(monkeypatch):
    monkeypatch.setenv("MONGO_URI", "mongodb://example.com:27017")
    dbclient = _reload()
    client = dbclient.get_client()
    seeds = client.delegate._topology_settings.seeds
    assert ("example.com", 27017) in seeds
