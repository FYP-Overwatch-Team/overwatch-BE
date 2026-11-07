import importlib.util
from pathlib import Path

from bson.binary import Binary

from app.db import mongo
from app.embeddings.service import decode, encode

spec = importlib.util.spec_from_file_location(
    "migrate_embedding_vectors", Path(__file__).resolve().parents[2] / "scripts" / "migrate_embedding_vectors.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def seed():
    await mongo.embeddings().insert_many([
        {"_id": "listed", "workspace_id": "ws1", "kind": "work_item", "ref_id": "a", "vector": [0.5, -1.0, 2.0]},
        {"_id": "packed", "workspace_id": "ws1", "kind": "work_item", "ref_id": "b", "vector": encode([1.0, 0.0, 0.0])},
    ])


async def test_a_dry_run_writes_nothing():
    await seed()

    assert await migration.migrate(apply=False) == ["vectors to pack: 1"]
    assert (await mongo.embeddings().find_one({"_id": "listed"}))["vector"] == [0.5, -1.0, 2.0]


async def test_apply_packs_listed_vectors_keeping_their_values_and_is_repeatable():
    await seed()

    await migration.migrate(apply=True)
    again = await migration.migrate(apply=True)

    vectors = {doc["_id"]: doc["vector"] async for doc in mongo.embeddings().find({})}
    assert all(isinstance(vector, bytes) for vector in vectors.values())
    assert decode(Binary(vectors["listed"], subtype=9)) == [0.5, -1.0, 2.0]
    assert decode(vectors["packed"]) == [1.0, 0.0, 0.0]
    assert again == ["vectors to pack: 0"]
