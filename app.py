"""
app.py — Flask review UI

Routes:
  GET  /                                     prompt + target-playlist form
  POST /generate                             run the pipeline, create a review session
  GET  /review/<session_id>                  results table (every track, every status)
  POST /review/<session_id>/track/<id>/approve
  POST /review/<session_id>/track/<id>/remove
  POST /review/<session_id>/track/<id>/regenerate
  POST /review/<session_id>/approve_all       approve every still-pending track
  POST /review/<session_id>/generate_more     generate more, informed by approved/rejected so far
  POST /review/<session_id>/finalize          write approved tracks to Spotify
  POST /review/<session_id>/cancel            discard the review, write nothing

Route handlers stay thin — HTTP concerns only (parsing the form, calling
pipeline.run_pipeline / session_store, rendering templates, redirecting).
All real logic lives in pipeline.py, review.py, session_store.py,
spotify_client.py, curation.py, modes.py, logging_utils.py, venue_config.py
— each independently tested without Flask or live credentials. This file
is where they all get wired together for a human to actually click through.

The target playlist (append to an existing one, or create a new one) is
decided on the GENERATE step, not finalize — that's what lets
curation.dedupe_against_playlist actually run during generation instead of
only being a theoretical feature. modes.resolve_target_playlist() resolves
precedence (explicit dropdown choice > mode's standing playlist > create
new); the result is stored via session_store.SessionStore's `target`
metadata so the finalize form on the review page stays correctly prefilled
across every review action, not just the first page view — and finalize()
still honors whatever's actually submitted there, so a human can override
the default at the last second.

Built for staff with little technical background, so mistakes redirect
back to a normal page with a plain-language flash message instead of
raising an HTTP error page. The only things that still render a dedicated
error page are cases a normal click can't cause (an unknown/stale review
link, or the server not being configured with credentials yet).

create_app() is a factory specifically so tests can inject fakes instead
of hitting real APIs — that's what makes the full request/response cycle
testable (test_app.py) without credentials. The `if __name__ == "__main__"`
block at the bottom is the only place real clients get built, and it isn't
runnable in this environment yet (see CLAUDE.md's "On hold" section).
"""

from __future__ import annotations

import os
import sys

from flask import Flask, abort, flash, redirect, render_template, request, url_for

import spotify_client as spotify_ops
from curation import RecentlyUsedLog
from logging_utils import RunLog, RunLogEntry
from modes import DAY_PART_PRESETS, build_generation_request, current_day_part, resolve_target_playlist
from pipeline import run_pipeline
from session_store import SessionStore
from venue_config import load_venue_config

MIN_TRACK_COUNT = 1
MAX_TRACK_COUNT = 100   # matches index.html's <input min/max> — enforced here too since that's
                        # only a client-side hint, easily bypassed by posting the form directly.

# open.spotify.com playlist URLs are deterministic from the id alone — this is
# the public web-player URL, unrelated to the api.spotify.com endpoint churn
# documented in spotify_client.py, so no extra API call is needed to build it.
SPOTIFY_WEB_PLAYLIST_URL = "https://open.spotify.com/playlist/{playlist_id}"

DEFAULT_SESSION_DB_PATH = "review_sessions.db"
DEFAULT_RUN_LOG_PATH = "run_log.jsonl"
DEFAULT_RECENTLY_USED_LOG_PATH = "recently_used.json"


def create_app(anthropic_client=None, spotify_client=None, session_store=None, run_log=None,
                venue_config=None, recently_used_log=None):
    app = Flask(__name__)
    # Only used to sign the flash-message cookie — this app has no logins or
    # sensitive session data, so a static dev key is fine locally. Override
    # via FLASK_SECRET_KEY if this ever runs somewhere shared/public.
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
    app.config["ANTHROPIC_CLIENT"] = anthropic_client
    app.config["SPOTIFY_CLIENT"] = spotify_client
    app.config["SESSION_STORE"] = session_store or SessionStore(DEFAULT_SESSION_DB_PATH)
    app.config["RUN_LOG"] = run_log or RunLog(DEFAULT_RUN_LOG_PATH)
    app.config["VENUE_CONFIG"] = venue_config or load_venue_config()
    app.config["RECENTLY_USED_LOG"] = recently_used_log or RecentlyUsedLog(DEFAULT_RECENTLY_USED_LOG_PATH)

    def _clients_or_503():
        anthropic_client = app.config["ANTHROPIC_CLIENT"]
        sp = app.config["SPOTIFY_CLIENT"]
        if anthropic_client is None or sp is None:
            abort(503, "The Spotify/Claude connection isn't set up yet.")
        return anthropic_client, sp

    def _load_session_or_404(session_id):
        session = app.config["SESSION_STORE"].load(session_id)
        if session is None:
            abort(404, "That review link isn't valid — it may have already been finalized.")
        return session

    @app.errorhandler(404)
    def not_found(error):
        return render_template("error.html", message=error.description), 404

    @app.errorhandler(503)
    def not_configured(error):
        return render_template("error.html", message=error.description), 503

    @app.get("/")
    def index():
        # Opportunistic cleanup — cheap enough to run on every page load,
        # no separate scheduler needed. Never let this break the page.
        try:
            app.config["SESSION_STORE"].prune()
        except Exception as e:
            print(f"  ! session prune failed: {e}", file=sys.stderr)

        sp = app.config["SPOTIFY_CLIENT"]
        playlists = []
        if sp is not None:
            try:
                playlists = spotify_ops.list_playlists(sp)
            except Exception as e:
                # Best-effort — a listing failure shouldn't block the whole
                # form, just the picker degrades to "create new" only. But
                # log it: this failing silently is exactly what hid a real
                # bug (GET /me/playlists' undocumented-here 50-item page
                # cap) behind an empty dropdown with zero visible error.
                print(f"  ! list_playlists failed, picker will be empty: {e}", file=sys.stderr)
                playlists = []

        return render_template(
            "index.html",
            modes=sorted(DAY_PART_PRESETS),
            current_mode=current_day_part(),
            playlists=playlists,
        )

    @app.post("/generate")
    def generate():
        vibe_prompt = request.form.get("vibe_prompt", "").strip()
        if not vibe_prompt:
            flash("Please describe the vibe you want before generating.", "error")
            return redirect(url_for("index"))

        try:
            track_count = int(request.form.get("track_count", 20))
        except ValueError:
            flash("Track count needs to be a number.", "error")
            return redirect(url_for("index"))
        if not (MIN_TRACK_COUNT <= track_count <= MAX_TRACK_COUNT):
            flash(f"Track count needs to be between {MIN_TRACK_COUNT} and {MAX_TRACK_COUNT}.", "error")
            return redirect(url_for("index"))

        mode = request.form.get("mode") or None
        allow_explicit = request.form.get("allow_explicit") == "on"
        prefer_less_popular = request.form.get("avoid_obvious") == "on"
        ignore_recently_used = request.form.get("ignore_recently_used") == "on"
        raw_cap = request.form.get("max_per_artist") or None
        max_per_artist = None
        if raw_cap:
            try:
                max_per_artist = int(raw_cap)
            except ValueError:
                flash("Max tracks per artist needs to be a number.", "error")
                return redirect(url_for("index"))
            if max_per_artist < 1:
                flash("Max tracks per artist needs to be 1 or more.", "error")
                return redirect(url_for("index"))

        explicit_playlist_id = request.form.get("playlist_id") or None
        requested_playlist_name = request.form.get("playlist_name") or None

        anthropic_client, sp = _clients_or_503()
        venue_config = app.config["VENUE_CONFIG"]

        target = resolve_target_playlist(
            mode=mode, explicit_playlist_id=explicit_playlist_id,
            standing_playlists=venue_config.standing_playlists,
        )

        existing_playlist_ids = set()
        if target.action == "append":
            try:
                existing_playlist_ids = spotify_ops.get_playlist_track_ids(sp, target.playlist_id)
            except Exception as e:
                print(f"  ! get_playlist_track_ids failed, dedupe skipped this run: {e}", file=sys.stderr)
                existing_playlist_ids = set()   # best-effort dedupe, never blocks generation
            target_name = None
        else:
            target_name = requested_playlist_name or target.playlist_name or "New Playlist"

        # "Seed playlist" grounding: whichever playlist this batch is being
        # appended to (picked explicitly, or via a mode's standing playlist)
        # doubles as a house-taste source automatically, on top of any
        # venue-wide ones from venue_config — pick an existing playlist and
        # its own songs are what get shown to the LLM as the style to match,
        # no separate "seed playlist" field needed.
        house_taste_sources = list(venue_config.house_taste_playlist_ids)
        if target.action == "append" and target.playlist_id not in house_taste_sources:
            house_taste_sources.append(target.playlist_id)

        house_taste = []
        if house_taste_sources:
            try:
                house_taste = spotify_ops.get_house_taste_sample(sp, house_taste_sources)
            except Exception as e:
                print(f"  ! get_house_taste_sample failed, generating with no grounding this run: {e}",
                      file=sys.stderr)
                house_taste = []   # best-effort grounding, never blocks generation

        # "Ignore recently-used" is a full bypass, not a shorter window — meant
        # for active testing/iteration against the same playlist within days,
        # where the normal 30-day venue-wide window (curation.DEFAULT_RECENT_WINDOW_DAYS)
        # would otherwise exclude tracks written just hours earlier in a prior
        # test run, starving repeated runs of their best/most obvious candidates.
        recent_log_ids = (
            set() if ignore_recently_used
            else app.config["RECENTLY_USED_LOG"].ids_used_within()
        )

        gen_request = build_generation_request(
            vibe_prompt, track_count, mode=mode, explicit_ok=allow_explicit,
            avoid_obvious=prefer_less_popular,
            house_taste=house_taste, blocklist=venue_config.blocklist,
        )

        try:
            result = run_pipeline(
                anthropic_client, sp, gen_request,
                allow_explicit=allow_explicit, max_per_artist=max_per_artist,
                existing_playlist_ids=existing_playlist_ids, recent_log_ids=recent_log_ids,
            )
        except Exception as e:
            # Real, not hypothetical: generator.py documents raising RuntimeError
            # after 3 failed attempts (Claude refusal/truncation/bad JSON) — this
            # is the one place in the pipeline actually likely to fail live.
            print(f"  ! run_pipeline failed: {e}", file=sys.stderr)
            flash("Something went wrong generating suggestions — please try again.", "error")
            return redirect(url_for("index"))

        session_id = app.config["SESSION_STORE"].create(result.kept, target={
            "action": target.action,
            "playlist_id": target.playlist_id,
            "playlist_name": target_name if target.action == "create" else None,
        })

        app.config["RUN_LOG"].append(RunLogEntry(
            vibe_prompt=vibe_prompt,
            requested_count=track_count,
            generated_count=result.generated_count,
            accepted_count=result.accepted_count,
            final_track_ids=[t.track_id for t in result.kept],
            action="pending_review",
            mode=mode,
            dropped_summary=result.house_rules.summary["dropped_by_reason"],
            resolver_dropped_summary=result.resolver_dropped_by_reason,
            avoid_obvious=prefer_less_popular,
            ignore_recently_used=ignore_recently_used,
        ))

        dropped = result.generated_count - len(result.kept)
        message = f"Found {len(result.kept)} track{'s' if len(result.kept) != 1 else ''} to review."
        if dropped > 0:
            message += f" ({dropped} suggestion{'s' if dropped != 1 else ''} didn't make the cut.)"
        flash(message, "success")

        return redirect(url_for("review", session_id=session_id))

    @app.get("/review/<session_id>")
    def review(session_id):
        session = _load_session_or_404(session_id)
        target = app.config["SESSION_STORE"].get_target(session_id) or {}
        return render_template(
            "review.html", session_id=session_id, items=session.items(),
            summary=session.summary(),
            target_playlist_id=target.get("playlist_id") or "",
            target_playlist_name=target.get("playlist_name") or "",
        )

    def _track_name_or_id(session, track_id):
        for item in session.items():
            if item.result.track_id == track_id:
                return item.result.track_name or track_id
        return track_id

    @app.post("/review/<session_id>/track/<track_id>/approve")
    def approve_track(session_id, track_id):
        session = _load_session_or_404(session_id)
        name = _track_name_or_id(session, track_id)
        try:
            session.approve(track_id)
        except KeyError:
            abort(404, "That track isn't part of this review anymore.")
        app.config["SESSION_STORE"].save(session_id, session)
        flash(f'Approved "{name}".', "success")
        return redirect(url_for("review", session_id=session_id, _anchor=f"track-{track_id}"))

    @app.post("/review/<session_id>/track/<track_id>/remove")
    def remove_track(session_id, track_id):
        session = _load_session_or_404(session_id)
        name = _track_name_or_id(session, track_id)
        try:
            session.remove(track_id, reason=request.form.get("reason") or None)
        except KeyError:
            abort(404, "That track isn't part of this review anymore.")
        app.config["SESSION_STORE"].save(session_id, session)
        flash(f'Removed "{name}".', "success")
        return redirect(url_for("review", session_id=session_id, _anchor=f"track-{track_id}"))

    @app.post("/review/<session_id>/track/<track_id>/regenerate")
    def regenerate_track(session_id, track_id):
        session = _load_session_or_404(session_id)
        name = _track_name_or_id(session, track_id)
        try:
            session.request_regenerate(track_id, note=request.form.get("note") or None)
        except KeyError:
            abort(404, "That track isn't part of this review anymore.")
        app.config["SESSION_STORE"].save(session_id, session)
        flash(f'Flagged "{name}" for regeneration.', "success")
        return redirect(url_for("review", session_id=session_id, _anchor=f"track-{track_id}"))

    @app.post("/review/<session_id>/approve_all")
    def approve_all(session_id):
        session = _load_session_or_404(session_id)
        count = session.approve_all_pending()
        app.config["SESSION_STORE"].save(session_id, session)
        if count:
            flash(f"Approved {count} remaining track{'s' if count != 1 else ''}.", "success")
        else:
            flash("Nothing left to approve.", "success")
        return redirect(url_for("review", session_id=session_id))

    @app.post("/review/<session_id>/generate_more")
    def generate_more(session_id):
        """Generate an additional batch of candidates mid-review and merge
        them into the SAME session as new pending rows — approved/removed/
        regenerate-requested tracks are left untouched. The generation is
        grounded on what's already happened in this review: approved
        tracks feed house-taste grounding (same mechanism as the seed-
        playlist feature, just sourced from this session instead of a
        Spotify playlist), and removed/regenerate-requested tracks become
        an explicit "don't suggest these again" signal via
        GenerationRequest.previously_rejected — see its docstring for why
        that's kept separate from the permanent, venue-wide blocklist.
        """
        session = _load_session_or_404(session_id)
        anthropic_client, sp = _clients_or_503()

        additional_prompt = request.form.get("additional_prompt", "").strip()
        if not additional_prompt:
            flash("Describe what you want before generating more.", "error")
            return redirect(url_for("review", session_id=session_id))

        try:
            track_count = int(request.form.get("track_count", 10))
        except ValueError:
            flash("Track count needs to be a number.", "error")
            return redirect(url_for("review", session_id=session_id))
        if not (MIN_TRACK_COUNT <= track_count <= MAX_TRACK_COUNT):
            flash(f"Track count needs to be between {MIN_TRACK_COUNT} and {MAX_TRACK_COUNT}.", "error")
            return redirect(url_for("review", session_id=session_id))

        allow_explicit = request.form.get("allow_explicit") == "on"
        prefer_less_popular = request.form.get("avoid_obvious") == "on"
        ignore_recently_used = request.form.get("ignore_recently_used") == "on"

        venue_config = app.config["VENUE_CONFIG"]
        target = app.config["SESSION_STORE"].get_target(session_id) or {}
        target_playlist_id = target.get("playlist_id")

        # Dedupe against every track already in THIS session (any status —
        # no point re-suggesting something already approved OR already
        # rejected) plus, if there's a target playlist, its existing tracks.
        existing_ids = {item.result.track_id for item in session.items()}
        if target_playlist_id:
            try:
                existing_ids |= spotify_ops.get_playlist_track_ids(sp, target_playlist_id)
            except Exception as e:
                print(f"  ! get_playlist_track_ids failed, dedupe skipped this round: {e}", file=sys.stderr)

        approved_lines = [
            f"{item.result.track_artists} - {item.result.track_name}"
            for item in session.items() if item.status == "approved"
        ]

        rejected_lines = []
        for item in session.items():
            if item.status == "removed":
                label = f"{item.result.track_artists} - {item.result.track_name}"
                if item.note:
                    label += f" (reason: {item.note})"
                rejected_lines.append(label)
            elif item.status == "regenerate_requested":
                label = f"{item.result.track_artists} - {item.result.track_name}"
                label += f" (wanted instead: {item.note})" if item.note else " (flagged for regeneration)"
                rejected_lines.append(label)

        house_taste_sources = list(venue_config.house_taste_playlist_ids)
        if target_playlist_id and target_playlist_id not in house_taste_sources:
            house_taste_sources.append(target_playlist_id)
        house_taste = []
        if house_taste_sources:
            try:
                house_taste = spotify_ops.get_house_taste_sample(sp, house_taste_sources)
            except Exception as e:
                print(f"  ! get_house_taste_sample failed, generating with no venue grounding "
                      f"this round: {e}", file=sys.stderr)
        # combine, don't replace — same pattern as the seed-playlist feature
        house_taste = house_taste + approved_lines

        recent_log_ids = (
            set() if ignore_recently_used
            else app.config["RECENTLY_USED_LOG"].ids_used_within()
        )

        gen_request = build_generation_request(
            additional_prompt, track_count, explicit_ok=allow_explicit,
            avoid_obvious=prefer_less_popular, house_taste=house_taste,
            blocklist=venue_config.blocklist, previously_rejected=rejected_lines,
        )

        try:
            result = run_pipeline(
                anthropic_client, sp, gen_request,
                allow_explicit=allow_explicit,
                existing_playlist_ids=existing_ids, recent_log_ids=recent_log_ids,
            )
        except Exception as e:
            print(f"  ! run_pipeline failed (generate_more): {e}", file=sys.stderr)
            flash("Something went wrong generating more suggestions — please try again.", "error")
            return redirect(url_for("review", session_id=session_id))

        if not result.kept:
            flash("No new suggestions made it through this round — try a different prompt.", "error")
            return redirect(url_for("review", session_id=session_id))

        session.add_tracks(result.kept)
        app.config["SESSION_STORE"].save(session_id, session)

        app.config["RUN_LOG"].append(RunLogEntry(
            vibe_prompt=additional_prompt,
            requested_count=track_count,
            generated_count=result.generated_count,
            accepted_count=result.accepted_count,
            final_track_ids=[t.track_id for t in result.kept],
            action="generate_more",
            target_playlist_id=target_playlist_id,
            dropped_summary=result.house_rules.summary["dropped_by_reason"],
            resolver_dropped_summary=result.resolver_dropped_by_reason,
            avoid_obvious=prefer_less_popular,
            ignore_recently_used=ignore_recently_used,
        ))

        flash(f"Added {len(result.kept)} more track{'s' if len(result.kept) != 1 else ''} to review.",
              "success")
        anchor = result.kept[0].track_id
        return redirect(url_for("review", session_id=session_id, _anchor=f"track-{anchor}"))

    @app.post("/review/<session_id>/finalize")
    def finalize(session_id):
        session = _load_session_or_404(session_id)
        _, sp = _clients_or_503()

        uris = session.final_uris()
        if not uris:
            flash("Approve at least one track before writing to Spotify.", "error")
            return redirect(url_for("review", session_id=session_id))

        explicit_playlist_id = request.form.get("playlist_id") or None
        playlist_name = request.form.get("playlist_name") or "New Playlist"

        try:
            if explicit_playlist_id:
                target_id = explicit_playlist_id
                action_taken = "append"
            else:
                playlist = spotify_ops.create_playlist(sp, playlist_name)
                target_id = playlist["id"]
                action_taken = "create"

            spotify_ops.add_tracks_to_playlist(sp, target_id, uris)
        except Exception as e:
            # A manually-typed playlist ID in the override field on the review
            # page is free text, never validated against a real playlist — and
            # any live Spotify write can fail on its own (network, rate limit,
            # a since-deleted playlist). This is the single most consequential
            # action in the app; it must never crash to a raw error page.
            print(f"  ! finalize write to Spotify failed: {e}", file=sys.stderr)
            flash("Couldn't write to Spotify — double check the playlist ID and try again.", "error")
            return redirect(url_for("review", session_id=session_id))

        track_ids = [u.rsplit(":", 1)[-1] for u in uris]
        recently_used_log = app.config["RECENTLY_USED_LOG"]
        recently_used_log.record(track_ids)
        recently_used_log.save()

        app.config["RUN_LOG"].append(RunLogEntry(
            vibe_prompt="(finalize)",
            requested_count=len(uris),
            generated_count=len(uris),
            accepted_count=len(uris),
            final_track_ids=track_ids,
            action=action_taken,
            target_playlist_id=target_id,
        ))
        app.config["SESSION_STORE"].delete(session_id)

        return render_template(
            "done.html", playlist_id=target_id, count=len(uris), action=action_taken,
            playlist_url=SPOTIFY_WEB_PLAYLIST_URL.format(playlist_id=target_id),
        )

    @app.post("/review/<session_id>/cancel")
    def cancel_review(session_id):
        # 404s on an unknown/already-finalized id, same as every other route
        # here — cancel is only ever a real action on a review that's still
        # actually in progress.
        _load_session_or_404(session_id)
        app.config["SESSION_STORE"].delete(session_id)
        flash("Review discarded — nothing was written to Spotify.", "success")
        return redirect(url_for("index"))

    return app


if __name__ == "__main__":
    import anthropic

    from spotify_client import get_client as get_real_spotify_client

    flask_app = create_app(
        anthropic_client=anthropic.Anthropic(),
        spotify_client=get_real_spotify_client(),
    )
    flask_app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
