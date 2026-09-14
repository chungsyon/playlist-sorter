"""Offline checks for the classification path.

    uv run python test_classify.py

No network, no framework. These cover the parts where a silent regression
would be invisible in the UI: the model's characterisation reaching the review
screen, artist-level tags staying labelled as artist-level, and the schema
still forcing the model to describe a song before it picks a playlist.
"""

from __future__ import annotations

from app.classify import NO_RESPONSE, _raw_names, _reason_text, _validate, classify_batch
from app.enrich import ARTIST_SCOPE, _top_tags
from app.models import Bucket, Track
from app.providers.base import RESPONSE_SCHEMA, Provider, ProviderError, grouped_response_schema
from app.router import ProviderRouter


def test_reason_carries_the_characterisation() -> None:
    """The song description is what makes a wrong assignment legible, so it has
    to survive into the reason column rather than being dropped after use."""
    both = _reason_text({"song": "Dreamy synthpop, mid-tempo", "reason": "hazy warmth suits Sunset"})
    assert both == "Dreamy synthpop, mid-tempo — hazy warmth suits Sunset", both

    # Either half missing still yields something readable, never a bare dash.
    assert _reason_text({"song": "Loud punk"}) == "Loud punk"
    assert _reason_text({"reason": "fits the brief"}) == "fits the brief"
    assert _reason_text({}) == ""

    # The column is 200 chars; overflow truncates instead of raising.
    assert len(_reason_text({"song": "x" * 300, "reason": "y" * 300})) == 200


def test_schema_makes_the_model_describe_before_choosing() -> None:
    """Ordering is the whole mechanism: emit the playlist first and the model
    justifies backwards. If this assertion goes, so does the accuracy gain."""
    item = RESPONSE_SCHEMA["items"]
    assert item["propertyOrdering"].index("song") < item["propertyOrdering"].index("playlists")
    assert "song" in item["required"]

    grouped = grouped_response_schema(["Use", "Vibe"])["items"]
    assert grouped["propertyOrdering"].index("song") < grouped["propertyOrdering"].index("picks")
    assert "song" in grouped["required"]
    # Every group still has to come back non-empty.
    assert grouped["properties"]["picks"]["required"] == ["Use", "Vibe"]
    assert grouped["properties"]["picks"]["properties"]["Use"]["minItems"] == 1


def test_artist_tags_stay_labelled() -> None:
    """Artist tags are weaker evidence than track tags. Losing the marker would
    quietly present an artist's general style as a fact about this song."""
    track = Track(video_id="v", title="Song", artists="Band",
                  tags=[ARTIST_SCOPE, "rock", "indie", "alternative", "electronic", "chill"])
    row = track.as_prompt_row(0)
    assert ARTIST_SCOPE in row, row
    # Six slots, so the marker does not cost a real tag.
    assert "chill" in row, row


def test_tag_filter_drops_noise() -> None:
    """Library tags say something about the tagger, not the record."""
    payload = {"toptags": {"tag": [
        {"name": "seen live", "count": 100},     # junk
        {"name": "Chill", "count": 50},
        {"name": "Band", "count": 80},           # the artist's own name
        {"name": "obscure opinion", "count": 2}, # below the threshold
    ]}}

    class _Resp:
        status_code = 200
        def json(self): return payload

    class _Client:
        def get(self, *a, **k): return _Resp()

    assert _top_tags(_Client(), {}, exclude="band") == ["chill"]
    # Artist-level counts are 0-100 popularity scores, so the threshold relaxes.
    assert "obscure opinion" in _top_tags(_Client(), {}, exclude="band", min_count=1)


def test_grouped_picks_flatten_by_bucket_not_by_key() -> None:
    """A name counts for the group its bucket declares, never for the key the
    model filed it under — otherwise mis-filing could fake a filled group."""
    assert sorted(_raw_names({"picks": {"Use": ["Work Out"], "Vibe": ["Chill"]}})) == ["Chill", "Work Out"]
    assert _raw_names({"playlists": ["Solo"]}) == ["Solo"]
    assert _raw_names({}) == []


def test_validate_snaps_names_and_flags_dropped_songs() -> None:
    buckets = [Bucket(playlist_id="1", name="Work Out"), Bucket(playlist_id="2", name="Chill")]
    tracks = [Track(video_id="a", title="A"), Track(video_id="b", title="B")]

    out = _validate(
        [{"idx": 0, "song": "Fast", "playlists": ["work out", "Nonexistent"],
          "confidence": 3.0, "reason": "drives"}],
        tracks, buckets, "gemini",
    )

    # Case drift snapped back, invented playlist dropped, confidence clamped.
    assert out[0].playlists == ["Work Out"], out[0].playlists
    assert out[0].confidence == 1.0
    assert out[0].reason == "Fast — drives"

    # The song the model skipped surfaces for review instead of vanishing.
    assert out[1].playlists == [] and out[1].confidence == 0.0


class _Canned(Provider):
    """A provider that hands back whatever text it was given."""

    def __init__(self, name: str, *replies: str) -> None:
        self.name = name
        self.replies = list(replies)
        self.calls = 0

    def available(self) -> bool:
        return True

    def generate_json(self, prompt: str, schema: dict | None = None) -> str:
        self.calls += 1
        if not self.replies:
            raise ProviderError("out of canned replies")
        return self.replies.pop(0)


def test_unparseable_reply_is_retried_not_fatal() -> None:
    """A provider answering with broken JSON has not answered.

    This killed a 2,111-song job at 1,500: parsing sat outside the router, so
    one malformed batch raised straight past the retry and the failover that
    exist precisely for this.
    """
    good = '[{"idx": 0, "song": "x", "playlists": [], "confidence": 0.5}]'

    # Retried on the same provider.
    flaky = _Canned("flaky", "{not json at all", good)
    items, who = ProviderRouter([flaky]).generate_items("p")
    assert who == "flaky" and items[0]["idx"] == 0
    assert flaky.calls == 2, flaky.calls

    # Failed over to the next provider when the first cannot be salvaged.
    broken, backup = _Canned("broken", "{[", "}}"), _Canned("backup", good)
    items, who = ProviderRouter([broken, backup]).generate_items("p")
    assert who == "backup", who
    assert broken.calls == 2, broken.calls


def test_all_providers_failing_raises_the_pause_signal() -> None:
    """Exhaustion must pause the job with its cursor intact, never crash it."""
    from app.router import AllProvidersExhausted
    try:
        ProviderRouter([_Canned("a", "{["), _Canned("b", "nope")]).generate_items("p")
    except AllProvidersExhausted:
        pass
    else:
        raise AssertionError("expected AllProvidersExhausted")


def test_skipped_songs_get_a_second_ask() -> None:
    """Big batches lose songs — the model just stops writing entries. Those are
    indistinguishable from unclassifiable ones by review time, so they get one
    more ask, and only they do: a re-ask of the whole batch would cost a request
    the daily cap may not have."""
    buckets = [Bucket(playlist_id="1", name="Work Out"), Bucket(playlist_id="2", name="Chill")]
    tracks = [Track(video_id=v, title=v) for v in ("a", "b", "c")]

    # First reply covers a and c; b is missing. Second reply supplies b.
    first = '[{"idx":0,"song":"x","playlists":["Work Out"],"confidence":0.9},' \
            ' {"idx":2,"song":"z","playlists":["Chill"],"confidence":0.8}]'
    second = '[{"idx":0,"song":"y","playlists":["Chill"],"confidence":0.7}]'

    provider = _Canned("gemini", first, second)
    out = classify_batch(ProviderRouter([provider]), tracks, buckets)

    assert provider.calls == 2, provider.calls
    assert [a.video_id for a in out] == ["a", "b", "c"]
    assert out[1].playlists == ["Chill"], out[1].playlists     # the straggler, recovered
    assert out[0].playlists == ["Work Out"]                     # first-pass answers kept
    assert out[2].playlists == ["Chill"]

    # Only the missing song is re-sent, not the whole batch.
    assert all(a.reason != NO_RESPONSE for a in out)


def test_a_wholly_empty_reply_is_not_re_asked() -> None:
    """Nothing coming back is a failed batch, not stragglers — the router has
    already spent its retries, so a re-ask would just burn another request."""
    buckets = [Bucket(playlist_id="1", name="Work Out")]
    tracks = [Track(video_id="a", title="A")]
    provider = _Canned("gemini", "[]", "[]")
    out = classify_batch(ProviderRouter([provider]), tracks, buckets)
    assert provider.calls == 1, provider.calls
    assert out[0].reason == NO_RESPONSE


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nAll checks passed.")
