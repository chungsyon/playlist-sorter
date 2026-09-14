"""Offline checks for the classification path.

    uv run python test_classify.py

No network, no framework. These cover the parts where a silent regression
would be invisible in the UI: the model's characterisation reaching the review
screen, artist-level tags staying labelled as artist-level, and the schema
still forcing the model to describe a song before it picks a playlist.
"""

from __future__ import annotations

from app.classify import _raw_names, _reason_text, _validate
from app.enrich import ARTIST_SCOPE, _top_tags
from app.models import Bucket, Track
from app.providers.base import RESPONSE_SCHEMA, grouped_response_schema


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nAll checks passed.")
