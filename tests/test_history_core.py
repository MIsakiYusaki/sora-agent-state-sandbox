"""Encrypted canonical history and actual Gateway routes; explicit isolated QA input."""
from unittest.mock import patch

import pytest

from tests import test_conversation_gateway as fixtures


@pytest.fixture
def gateway():
    case = fixtures.ConversationGatewayTests(methodName="test_first_run_history_is_an_empty_page")
    case.setUp()
    yield case
    case.tearDown()


def seed(case, external_id, contents, *, user="user-a"):
    binding = case.registry.ensure_conversation(profile_id="profile-a", subject_id=user, agent_id="sora", external_conversation_id=external_id)
    journal = case.runtime.journal
    journal.import_legacy_history(binding.runtime_conversation_id, [{"role":"user", "content":contents[0]}])
    first = journal.history_page(binding.runtime_conversation_id, limit=1)[0]
    with journal.transaction() as connection:
        template = dict(connection.execute("SELECT * FROM runtime_turn WHERE turn_id=?", (first["turn_id"],)).fetchone())
        turn_id = first["turn_id"]
        for sequence, text in enumerate(contents[1:],2):
            if sequence % 2:
                turn_id = f"qa-turn-{external_id}-{sequence}"
                row = {**template, "turn_id": turn_id, "request_id": turn_id,
                    "input_cipher": journal.cipher.encrypt_text("", associated_data=f"turn-input:{turn_id}"),
                    "result_cipher": journal.cipher.encrypt_json({"qa_history":True}, associated_data=f"turn-result:{turn_id}")}
                columns = list(row)
                connection.execute(f"INSERT INTO runtime_turn({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", [row[column] for column in columns])
            history_id = f"qa-{external_id}-{sequence}"
            connection.execute("INSERT INTO runtime_short_turn(history_id,conversation_id,turn_id,sequence,role,content_cipher,content_hash,created_at_ms) VALUES(?,?,?,?,?,?,?,?)",
                (history_id,binding.runtime_conversation_id,turn_id,sequence,"user" if sequence%2 else "sora",
                 journal.cipher.encrypt_text(text,associated_data=f"history:{history_id}"),"qa-only",first["created_at_ms"]+sequence))
    return binding


def test_unfiltered_index_decrypts_only_visible_summaries_and_keeps_history_encrypted(gateway):
    binding = seed(gateway,"large",["first title"] + [f"private middle {n}" for n in range(1000)] + ["last preview"])
    cipher = gateway.runtime.journal.cipher
    with patch.object(cipher,"decrypt_text",wraps=cipher.decrypt_text) as decrypt:
        response=gateway.client.get("/v1/conversations",headers=gateway.headers())
    assert response.status_code==200
    row=response.json()["items"][0]
    assert (row["title"],row["preview"],row["message_count"]) == ("first title","last preview",1002)
    assert decrypt.call_count == 2
    stored=gateway.runtime.journal._conn.execute("SELECT content_cipher FROM runtime_short_turn WHERE conversation_id=?",(binding.runtime_conversation_id,)).fetchall()
    assert all("private middle" not in str(item[0]) for item in stored)


def test_full_body_search_reaches_old_page_and_treats_query_as_literal(gateway):
    seed(gateway,"body",["title", "unique Straße 100%_literal"]+[f"ordinary {n}" for n in range(1000)]+["latest summary"])
    seed(gateway,"foreign",["title","foreign only secret"],user="other-user")
    for query in ("strasse", "100%_literal"):
        result=gateway.client.get("/v1/conversations",params={"query":query},headers=gateway.headers())
        assert result.status_code==200
        assert [row["conversation_id"] for row in result.json()["items"]] == ["body"]
        assert result.json()["items"][0]["message_count"] == 1003
    assert gateway.client.get("/v1/conversations",params={"query":"foreign only secret"},headers=gateway.headers()).json()["items"] == []
    assert gateway.client.get("/v1/conversations",headers=gateway.headers(gateway.foreign_token)).status_code==403


@pytest.mark.parametrize("count,limit",[(100,50),(400,200),(401,200),(50,50)])
def test_exact_history_end_and_multiple_pages_have_no_omissions_or_duplicates(gateway,count,limit):
    seed(gateway,"pages",[f"QA message {n}" for n in range(count)])
    before=None; sequences=[]; pages=0
    while True:
        params={"limit":limit}
        if before is not None: params["before_sequence"]=before
        response=gateway.client.get("/v1/conversations/pages/history",params=params,headers=gateway.headers())
        assert response.status_code==200
        data=response.json(); pages+=1
        sequences=data["items"] + sequences
        if data["next_before_sequence"] is None: break
        assert before is None or data["next_before_sequence"] < before
        before=data["next_before_sequence"]
    assert [item["sequence"] for item in sequences] == list(range(1,count+1))
    assert pages == (count+limit-1)//limit


def test_empty_search_batches_keep_a_real_continuation_to_older_conversations(gateway):
    for n in range(105): seed(gateway,f"conversation-{n:03}",["ordinary title", "needle only in oldest" if n==0 else "ordinary body"])
    first=gateway.client.get("/v1/conversations",params={"query":"needle only"},headers=gateway.headers()).json()
    assert first["items"]==[] and first["next_offset"]==100
    second=gateway.client.get("/v1/conversations",params={"query":"needle only","offset":first["next_offset"]},headers=gateway.headers()).json()
    assert [row["conversation_id"] for row in second["items"]] == ["conversation-000"]
    assert second["next_offset"] is None


def test_recent_activity_is_ordered_before_pagination_not_only_within_each_page(gateway):
    oldest = seed(gateway,"oldest-resumed",["Old title", "Latest resumed content"])
    for n in range(104): seed(gateway,f"newer-{n:03}",["New title", "Earlier content"])
    with gateway.runtime.journal.transaction() as connection:
        connection.execute("UPDATE runtime_short_turn SET created_at_ms=created_at_ms+100000 WHERE conversation_id=? AND sequence=2", (oldest.runtime_conversation_id,))
    first = gateway.client.get("/v1/conversations", headers=gateway.headers()).json()
    second = gateway.client.get("/v1/conversations", params={"offset":first["next_offset"]}, headers=gateway.headers()).json()
    assert first["items"][0]["conversation_id"] == "oldest-resumed"
    ids = [row["conversation_id"] for row in first["items"] + second["items"]]
    assert len(ids) == len(set(ids)) == 105
