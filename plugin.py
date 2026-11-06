"""Clean Titles: renames VOD movies and series to their official titles.

Each Movie/Series with a TMDB ID is looked up on TMDB (one request per title,
which also returns the IMDb ID) and renamed to the official title in the chosen
language, which is the title IMDb lists for English. Provider decorations such as
"EN - ", "AMZ - " or " (US)" disappear.

Why TMDB and not IMDb directly: IMDb has no public API, and Dispatcharr's
catalogue stores TMDB IDs for nearly every title (and almost no IMDb IDs).

Safe by design:
- Dry run by default.
- Only titles that have a TMDB ID are touched. Dispatcharr's VOD refresh matches
  those rows by TMDB ID, never by name, and never writes the name of an existing
  row back, so a rename survives refreshes and creates no duplicates. Titles
  without a TMDB ID are matched by name+year during refresh, so renaming them
  would create duplicates: they are left alone.
- The provider's original name is kept in custom_properties["imdb_titles"], so
  Revert All puts everything back.
- A rename that would collide with another row (same name, year and language,
  which Dispatcharr forbids) is skipped and counted, never forced.

Runs in a Celery worker (the click only queues it): a full pass over tens of
thousands of titles takes longer than a web request may wait.
"""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.json"), encoding="utf-8") as _f:
    _MANIFEST = json.load(_f)

LOGGER_NAME = "apps.plugins.clean_titles"  # under "apps" so it follows Dispatcharr's LOG_LEVEL
# Keeps its old name: renamed titles already store their originals under this key,
# so changing it would break Revert All Renames.
MARK = "imdb_titles"                      # custom_properties key holding the original name
TMDB = "https://api.themoviedb.org/3"


def _aware(epoch):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _release_db_connections():
    """Return this thread's DB connection to Dispatcharr's pool (see vod_probe for why).

    Never inside a transaction: Dispatcharr calls plugin code from within its own
    atomic blocks (e.g. while saving enable/disable), and closing the connection
    there aborts its save with "Cannot open a new connection in an atomic block"."""
    from django.db import connection, connections

    if connection.in_atomic_block:
        return
    connections.close_all()


class _RateLimiter:
    def __init__(self, per_second):
        self.interval = 1.0 / max(1.0, float(per_second))
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            time.sleep(delay)


class _State:
    """Status, stop flag and recheck flag, in a folder next to the plugin's
    (an update replaces the plugin folder; this survives it)."""

    STALE_SECONDS = 900

    def __init__(self):
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.dir = os.path.join(os.path.dirname(plugin_dir), "imdb_titles_data")
        os.makedirs(self.dir, exist_ok=True)
        self.status_path = os.path.join(self.dir, "status.json")
        self.stop_path = os.path.join(self.dir, "stop")
        self.recheck_path = os.path.join(self.dir, "recheck")

    def read(self):
        try:
            with open(self.status_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def write(self, status):
        status["updated"] = time.time()
        tmp = self.status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f)
        os.replace(tmp, self.status_path)

    def busy(self):
        s = self.read()
        return bool(s.get("running")) and time.time() - s.get("updated", 0) < self.STALE_SECONDS

    def flag(self, path, on):
        if on:
            open(path, "w").close()
        elif os.path.exists(path):
            os.remove(path)

    def has(self, path):
        return os.path.exists(path)


def _service_list(value):
    """Service names from a multi-select list, or comma-separated text from before it."""
    items = value.split(",") if isinstance(value, str) else value or []
    return [x.strip() for x in items if x.strip()]


class Plugin:
    name = _MANIFEST["name"]
    version = _MANIFEST["version"]
    description = _MANIFEST["description"]
    author = _MANIFEST["author"]

    @property
    def fields(self):
        """Manifest fields with their lists filled, read when the plugin loads."""
        fields = json.loads(json.dumps(_MANIFEST["fields"]))
        try:
            settings = self._saved_settings()
        except Exception:
            settings = {}
        services = self._region_services(settings)
        for field in fields:
            saved = settings.get(field["id"])
            if field["id"] in ("language", "platform_region") and saved:
                if str(saved).strip() not in {o["value"] for o in field["options"]}:
                    field["options"].append({"value": str(saved).strip(), "label": str(saved).strip()})
            elif field["id"] == "platform_order":
                # Saved services stay offered: Dispatcharr refuses to save a multi-select
                # holding a value it doesn't offer.
                names = dict.fromkeys([*services, *field["default"], *_service_list(saved)])
                field["options"] = [{"value": n, "label": n} for n in names]
        return fields

    @staticmethod
    def _saved_settings():
        """Saved settings, moving a service order saved as text to a list: the settings
        page crashes on text in a multi-select."""
        from apps.plugins.models import PluginConfig

        config = PluginConfig.objects.filter(key="clean_titles").first()
        settings = (config.settings if config else None) or {}
        if isinstance(settings.get("platform_order"), str):
            settings["platform_order"] = _service_list(settings["platform_order"])
            config.save(update_fields=["settings"])
        return settings

    _BACKGROUND = {"rename_run": "Rename Run", "revert_all": "Revert All Renames", "tag_duplicates": "Tag Duplicates",
                   "unify_series": "Unify Series", "revert_unify": "Revert Unify", "fix_images": "Fix Images",
                   "group_categories": "Streaming Categories", "revert_groups": "Revert Streaming Categories"}

    def __init__(self):
        self.state = _State()

    # --- dispatch -------------------------------------------------------------

    def run(self, action_id, params, context):
        settings = context.get("settings", {}) or {}
        try:
            if action_id in self._BACKGROUND:
                # These actions are declared "async" in plugin.json, so Dispatcharr
                # already runs them in its own plugin worker task (button clicks and
                # the m3u_refresh event alike): run the work directly here.
                from_event = bool((params or {}).get("event"))
                if action_id == "rename_run" and from_event:
                    return self._on_m3u_refresh(settings, params)
                if self.state.busy():
                    return {"status": "error", "message": "A run is already in progress. See Status, or Stop Run."}
                has_key = bool((settings.get("tmdb_api_key") or "").strip())
                if action_id == "fix_images":
                    return self._fix_images(settings)
                if action_id == "group_categories":
                    return self._group_categories(settings)
                if action_id == "revert_groups":
                    return self._revert_groups()
                if action_id == "rename_run" and not has_key:
                    return {"status": "error", "message": "Set your TMDB API key in the settings first."}
                if action_id == "rename_run":
                    return self._rename_run(settings)
                if action_id == "tag_duplicates":
                    dry = bool(settings.get("dry_run", True))
                    summary = self._describe_tags(self._tag_duplicates(dry), dry)
                    self._notify(summary, False, title="Clean Titles: duplicates tagged" if not dry else "Clean Titles: duplicate tags (dry run)")
                    return {"status": "ok", "message": summary}
                if action_id == "unify_series":
                    return self._unify_series(settings)
                if action_id == "revert_unify":
                    return self._revert_unify()
                return self._revert_all()
            if action_id == "status":
                return {"status": "ok", "message": self._describe(self.state.read())}
            if action_id == "stop_run":
                if not self.state.busy():
                    return {"status": "ok", "message": "No run in progress."}
                self.state.flag(self.state.stop_path, True)
                return {"status": "ok", "message": "Stopping after the title in progress."}
            if action_id == "recheck_all":
                self.state.flag(self.state.recheck_path, True)
                return {"status": "ok", "message": "The next Rename Run will look up already-renamed titles again too."}
            return {"status": "error", "message": f"Unknown action '{action_id}'"}
        finally:
            _release_db_connections()

    def stop(self, context=None):
        """Dispatcharr's lifecycle hook, called when the plugin is disabled (inside
        Dispatcharr's own transaction). Only asks a running job to stop: no DB access."""
        if self.state.busy():
            self.state.flag(self.state.stop_path, True)

    # --- TMDB -----------------------------------------------------------------

    @staticmethod
    def _lookup(session, key, kind, tmdb_id, language, limiter):
        """Returns (title, year, imdb_id) or None when TMDB has no such title."""
        url = f"{TMDB}/{'movie' if kind == 'movie' else 'tv'}/{tmdb_id}"
        params = {"language": language, "append_to_response": "external_ids"}
        headers = {"Accept": "application/json"}
        if key.startswith("eyJ"):  # v4 read access token
            headers["Authorization"] = f"Bearer {key}"
        else:                      # v3 API key
            params["api_key"] = key
        for attempt in range(4):
            limiter.wait()
            r = session.get(url, params=params, headers=headers, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2)) + attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 401:
                raise PermissionError("TMDB rejected the API key (401).")
            r.raise_for_status()
            d = r.json()
            title = (d.get("title") or d.get("name") or "").strip()
            date = d.get("release_date") or d.get("first_air_date") or ""
            year = int(date[:4]) if date[:4].isdigit() else None
            imdb = d.get("imdb_id") or (d.get("external_ids") or {}).get("imdb_id")
            return title, year, imdb
        raise RuntimeError("TMDB kept rate-limiting (429).")

    @staticmethod
    def _format(title, year, fmt):
        return f"{title} ({year})" if fmt == "title_year" and year else title

    # --- rename run -------------------------------------------------------------

    def _rename_run(self, settings, account=None):
        import logging

        import requests
        from django.db import IntegrityError, transaction
        from django.db.models import Q
        from apps.vod.models import Movie, Series

        log = logging.getLogger(LOGGER_NAME)
        key = (settings.get("tmdb_api_key") or "").strip()
        if not key:
            return {"status": "error", "message": "Set your TMDB API key in the settings first."}
        dry = bool(settings.get("dry_run", True))
        scope = settings.get("scope", "both")
        fmt = settings.get("name_format", "title")
        language = (settings.get("language") or "en-US").strip()
        limiter = _RateLimiter(settings.get("requests_per_second") or 20)
        recheck = self.state.has(self.state.recheck_path)

        kinds = [k for k in ("movie", "series") if scope in ("both", "movies" if k == "movie" else "series")]
        models = {"movie": Movie, "series": Series}

        todo = []
        for kind in kinds:
            qs = models[kind].objects.exclude(Q(tmdb_id__isnull=True) | Q(tmdb_id=""))
            if account is not None:  # after an M3U refresh: only that account's titles
                qs = qs.filter(m3u_relations__m3u_account_id=account.id).distinct()
            for pk, tmdb_id, cp in qs.values_list("id", "tmdb_id", "custom_properties").iterator(chunk_size=2000):
                mark = (cp or {}).get(MARK) if isinstance(cp, dict) else None
                if mark and mark.get("applied_name") and not recheck and (dry or mark.get("format") == fmt and mark.get("language") == language):
                    continue
                todo.append((kind, pk, tmdb_id))

        st = {
            "running": True, "action": "rename_run", "dry_run": dry, "started": time.time(), "total": len(todo),
            "processed": 0, "renamed": 0, "unchanged": 0, "not_found": 0, "conflicts": 0, "errors": 0,
            "samples": [], "stopped": False, "message": "",
        }
        self.state.flag(self.state.stop_path, False)
        self.state.write(st)
        if account is not None:
            st["account"] = account.name
        log.info("Rename run started: %d titles to look up (dry_run=%s, scope=%s%s)", len(todo), dry, scope,
                 f", account={account.name}" if account is not None else "")

        session = requests.Session()
        fatal = None
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(self._lookup, session, key, k, t, language, limiter): (k, pk) for k, pk, t in todo}
                for fut in as_completed(futures):
                    kind, pk = futures[fut]
                    st["processed"] += 1
                    try:
                        found = fut.result()
                        if found is None or not found[0]:
                            st["not_found"] += 1
                        else:
                            self._apply(models[kind], kind, pk, found, fmt, language, dry, st, transaction, IntegrityError)
                    except PermissionError as exc:
                        fatal = str(exc)
                    except Exception as exc:  # noqa: BLE001 - counted, logged, run continues
                        st["errors"] += 1
                        if st["errors"] <= 5:
                            log.warning("%s %s: lookup/rename failed: %s", kind, pk, exc)
                    if fatal or self.state.has(self.state.stop_path):
                        st["stopped"] = True
                        for f in futures:
                            f.cancel()
                        break
                    if st["processed"] % 100 == 0:
                        self.state.write(st)
        finally:
            _release_db_connections()

        if not st["stopped"] and recheck:
            self.state.flag(self.state.recheck_path, False)
        if not dry and not fatal and not st["stopped"]:
            try:
                st["tags"] = self._tag_duplicates(False)
            except Exception as exc:  # noqa: BLE001 - renames are done; report and carry on
                log.warning("Tagging duplicates failed: %s", exc)
        st["running"] = False
        st["finished"] = time.time()
        st["message"] = fatal or ""
        self.state.write(st)
        self.state.flag(self.state.stop_path, False)
        summary = self._describe(st)
        log.info("Rename run finished: %s", summary.replace("\n", " | "))
        self._notify(summary, bool(fatal or st["stopped"]))
        return {"status": "error" if fatal else "ok", "message": summary}

    def _apply(self, model, kind, pk, found, fmt, language, dry, st, transaction, IntegrityError):
        title, year, imdb = found
        obj = model.objects.only("id", "name", "custom_properties").get(pk=pk)
        new_name = self._format(title, year, fmt)[:255]
        cp = obj.custom_properties if isinstance(obj.custom_properties, dict) else {}
        mark = cp.get(MARK) or {}
        original = mark.get("original_name") or obj.name

        new_mark = dict(mark, original_name=original, applied_name=new_name, base_name=new_name, tag=None,
                        imdb_id=imdb, format=fmt, language=language, at=int(time.time()))

        if new_name == obj.name:
            st["unchanged"] += 1
            if not dry and mark.get("format") != fmt:
                cp = dict(cp)
                cp[MARK] = new_mark
                model.objects.filter(pk=pk).update(custom_properties=cp)
            return

        if len(st["samples"]) < 15:
            st["samples"].append(f"{obj.name}  ->  {new_name}")
        if dry:
            st["renamed"] += 1
            return
        cp = dict(cp)
        cp[MARK] = new_mark
        try:
            with transaction.atomic():
                model.objects.filter(pk=pk).update(name=new_name, custom_properties=cp)
            st["renamed"] += 1
        except IntegrityError:
            st["conflicts"] += 1  # another row already has this name+year+language

    # --- duplicates ----------------------------------------------------------------

    # Provider prefix before " - ": "EN - Zero Day (2025) (US)" -> "EN", "4K-D+ - Zootopia+" -> "4K-D+".
    _PREFIX = __import__("re").compile(r"^\s*([A-Za-z0-9+&|!]+(?:-[A-Za-z0-9+&|!]+)*)\s+-\s+\S")
    _FOURK = __import__("re").compile(r"^4K[-\s]*", __import__("re").I)

    @classmethod
    def _tags_for(cls, originals):
        """Tag per row of one duplicate group. "4K" is dropped from a tag ("4K-D+" -> "D+")
        unless that would give two rows of the group the same tag ("4K-D+" next to "D+")."""
        full = []
        for name in originals:
            m = cls._PREFIX.match(name or "")
            full.append(m.group(1) if m else None)
        short = []
        for t in full:
            s = cls._FOURK.sub("", t) if t else None
            short.append(s or t)  # a bare "4K" tag stays "4K"
        tags = []
        for i, s in enumerate(short):
            clash = s is not None and any(j != i and short[j] == s for j in range(len(short)))
            tags.append(full[i] if clash else s)
        return tags

    def _tag_duplicates(self, dry):
        """Rows sharing a TMDB ID (Dispatcharr keeps one row per language, and some
        providers' categories split a title into an '' and an untagged language row)
        all end up with the same clean name. Give each a short provider tag:
        "Zero Day [EN]" / "Zero Day [NF]"."""
        import logging

        from django.db import IntegrityError, transaction
        from django.db.models import Count
        from apps.vod.models import Movie, Series

        log = logging.getLogger(LOGGER_NAME)
        res = {"groups": 0, "tagged": 0, "ambiguous": 0, "conflicts": 0, "samples": [], "ambiguous_samples": []}
        try:
            for model in (Movie, Series):
                dup_ids = list(
                    model.objects.filter(custom_properties__has_key=MARK)
                    .exclude(tmdb_id__isnull=True).exclude(tmdb_id="")
                    .values("tmdb_id").annotate(n=Count("id")).filter(n__gt=1)
                    .values_list("tmdb_id", flat=True)
                )
                for tmdb_id in dup_ids:
                    rows = list(model.objects.filter(tmdb_id=tmdb_id).only("id", "name", "custom_properties").order_by("id"))
                    marks = [(r.custom_properties or {}).get(MARK) if isinstance(r.custom_properties, dict) else None for r in rows]
                    if not all(m and m.get("applied_name") for m in marks):
                        continue  # only tag groups whose rows were all renamed by this plugin
                    res["groups"] += 1
                    tags = self._tags_for([m.get("original_name") for m in marks])
                    known = [t for t in tags if t]
                    if len(known) != len(set(known)) or not known:
                        # e.g. the provider lists "EN - Ghosts (2021) (US)" twice: a tag
                        # can't tell those rows apart, so don't add one.
                        tags = [None] * len(rows)
                        res["ambiguous"] += 1
                        if len(res["ambiguous_samples"]) < 5:
                            res["ambiguous_samples"].append(" | ".join(m.get("original_name", "") for m in marks))
                    for row, mark, tag in zip(rows, marks, tags):
                        base = mark.get("base_name") or mark.get("applied_name") or row.name
                        new_name = (f"{base} [{tag}]" if tag else base)[:255]
                        if new_name == row.name:
                            continue
                        if len(res["samples"]) < 15:
                            res["samples"].append(f"{mark.get('original_name')}  ->  {new_name}")
                        if dry:
                            res["tagged"] += 1
                            continue
                        cp = dict(row.custom_properties)
                        cp[MARK] = dict(mark, base_name=base, applied_name=new_name, tag=tag)
                        try:
                            with transaction.atomic():
                                model.objects.filter(pk=row.pk).update(name=new_name, custom_properties=cp)
                            res["tagged"] += 1
                        except IntegrityError:
                            res["conflicts"] += 1
        finally:
            _release_db_connections()
        log.info("Duplicate tagging (dry_run=%s): %s", dry, {k: v for k, v in res.items() if not k.endswith("samples")})
        return res

    @staticmethod
    def _describe_tags(res, dry):
        lines = [
            f"{'Dry run: would tag' if dry else 'Tagged'} {res['tagged']} titles in {res['groups']} duplicate groups"
            f" (same TMDB ID). {res['ambiguous']} groups had no distinguishing provider prefix,"
            f" {res['conflicts']} skipped (name already taken)."
        ]
        if res["samples"]:
            lines.append("Examples:")
            lines.extend(f"  {s}" for s in res["samples"])
        if res["ambiguous_samples"]:
            lines.append("Could not tell apart (originals):")
            lines.extend(f"  {s}" for s in res["ambiguous_samples"])
        return "\n".join(lines)

    # --- after an M3U refresh ----------------------------------------------------------
    # The m3u_refresh event names the refreshed account. Only accounts with "Enable VOD
    # Scanning" on (and of Xtream type, the only kind Dispatcharr scans VOD for) are
    # handled. Dispatcharr queues that account's VOD scan right AFTER sending the event,
    # so the plugin first waits for the scan to finish (otherwise this refresh's new
    # titles would only be renamed at the next one), then fixes posters and renames that
    # account's titles. A refresh arriving while a run is busy is queued, not dropped.

    VOD_WAIT_START = 300    # s to wait for the VOD scan to show up
    VOD_WAIT_MAX = 3600     # s to wait for it to finish

    @staticmethod
    def _event_account(params):
        from apps.m3u.models import M3UAccount

        name = ((params or {}).get("payload") or {}).get("account_name")
        return M3UAccount.objects.filter(name=name).first() if name else None

    @staticmethod
    def _vod_enabled(account):
        from apps.m3u.models import M3UAccount

        cp = account.custom_properties
        if isinstance(cp, str):
            try:
                cp = json.loads(cp)
            except ValueError:
                cp = {}
        return bool((cp or {}).get("enable_vod")) and account.account_type == M3UAccount.Types.XC

    def _pending(self, add=None, take=False):
        """Accounts whose refresh arrived while a run was busy (ids in a small file)."""
        path = os.path.join(self.state.dir, "pending_accounts.json")
        with self._pending_lock:
            try:
                with open(path, encoding="utf-8") as f:
                    ids = json.load(f)
            except (OSError, ValueError):
                ids = []
            if add is not None and add not in ids:
                ids.append(add)
            out = ids
            if take:
                out, ids = ids[:1], ids[1:]
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(ids, f)
            os.replace(path + ".tmp", path)
        return out

    _pending_lock = threading.Lock()

    def _vod_scan_running(self, account_id):
        """True/False whether Dispatcharr's VOD scan task for this account is queued or
        running; None when the task workers didn't answer."""
        import re

        try:
            from dispatcharr.celery import app

            insp = app.control.inspect(timeout=3)
            answered = False
            for getter in (insp.active, insp.reserved, insp.scheduled):
                res = getter()
                if res is None:
                    continue
                answered = True
                for tasks in res.values():
                    for t in tasks or []:
                        req = t.get("request", t) if isinstance(t, dict) else {}
                        if not str(req.get("name", "")).endswith("refresh_vod_content"):
                            continue
                        if re.findall(r"\d+", str(req.get("args")))[:1] == [str(account_id)]:
                            return True
            return False if answered else None
        except Exception:  # noqa: BLE001
            return None

    def _wait_for_vod_scan(self, account, since, log):
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation

        st = {"running": True, "action": "waiting", "account": account.name, "started": time.time(), "stopped": False}
        self.state.flag(self.state.stop_path, False)
        seen = False
        while time.time() - since < self.VOD_WAIT_MAX:
            if self.state.has(self.state.stop_path):
                st.update(running=False, stopped=True, finished=time.time())
                self.state.write(st)
                self.state.flag(self.state.stop_path, False)
                return False
            self.state.write(st)  # keeps the run "busy" (and Status informative) while waiting
            running = self._vod_scan_running(account.id)
            if running:
                seen = True
            elif seen:
                return True  # it ran and is done
            elif time.time() - since > self.VOD_WAIT_START:
                # Never saw it: it may have finished before we looked (the scan marks every
                # title it sees), or the workers can't be asked. Go ahead either way.
                touched = any(rel.objects.filter(m3u_account=account, last_seen__gte=_aware(since)).exists()
                              for rel in (M3UMovieRelation, M3USeriesRelation))
                log.info("VOD scan of %s %s; renaming now.", account.name,
                         "already finished" if touched else "not seen (workers didn't report it)")
                return True
            time.sleep(10)
        log.warning("VOD scan of %s still running after %d s; renaming now.", account.name, self.VOD_WAIT_MAX)
        return True

    def _on_m3u_refresh(self, settings, params):
        import logging

        log = logging.getLogger(LOGGER_NAME)
        account = self._event_account(params)
        if account is None:
            return {"status": "ok", "message": "Refreshed M3U account not found; nothing renamed."}
        if not self._vod_enabled(account):
            return {"status": "ok", "message": f"{account.name}: Enable VOD Scanning is off; nothing renamed."}
        if self.state.busy():
            self._pending(add=account.id)
            return {"status": "ok", "message": f"{account.name}: a run is in progress; queued for after it."}
        since = time.time()
        messages = []
        while account is not None:
            if not self._wait_for_vod_scan(account, since, log):
                self._pending(add=account.id)  # stopped (e.g. plugin reloaded): do it next time
                messages.append(f"{account.name}: stopped while waiting for its VOD scan; queued for the next refresh.")
                break
            messages.append(self._group_categories(settings, notify=False)["message"])
            messages.append(self._fix_images(settings, notify=False, account=account)["message"])
            if (settings.get("tmdb_api_key") or "").strip():
                messages.append(self._rename_run(settings, account=account)["message"])
            nxt = self._pending(take=True)
            account = None
            if nxt:
                from apps.m3u.models import M3UAccount

                account = M3UAccount.objects.filter(pk=nxt[0]).first()
                since = time.time() - 60  # its scan was queued a while ago
        return {"status": "ok", "message": "\n".join(messages) or "Nothing to do."}

    # --- images ---------------------------------------------------------------------
    # Some providers point posters at their own image server; when that server is down
    # the catalogue shows blank cards. The file names are usually TMDB's own poster
    # files ("/images/movies/y6tSZ13...jpg"), so the same image is on image.tmdb.org;
    # otherwise the poster is looked up by the title's TMDB ID. Only the title's logo
    # reference changes (never streams). Each refresh points the title back at the
    # provider's image, so this also runs after every M3U refresh.

    _TMDB_IMG = "https://image.tmdb.org/t/p/w600_and_h900_bestv2/"
    _TMDB_FILE = __import__("re").compile(r"^[A-Za-z0-9]{20,40}\.(?:jpe?g|png)$")

    @staticmethod
    def _dead_hosts(urls_by_host, log):
        """Hosts where none of a few sample images can even be connected to."""
        import random
        import requests

        dead = set()
        for host, urls in urls_by_host.items():
            if not host or "tmdb.org" in host:
                continue
            alive = False
            for url in random.sample(urls, min(3, len(urls))):
                try:
                    requests.get(url, timeout=6, stream=True, headers={"User-Agent": "Mozilla/5.0"}).close()
                    alive = True  # it answered (even a 404 for one file means the server is up)
                    break
                except (requests.ConnectionError, requests.Timeout):
                    continue
                except Exception:  # noqa: BLE001 - e.g. a malformed URL: not a host problem
                    alive = True
                    break
            if not alive:
                dead.add(host)
        if dead:
            log.info("Image servers not answering: %s", ", ".join(sorted(dead)))
        return dead

    def _fix_images(self, settings, notify=True, account=None):
        import logging
        from urllib.parse import urlparse

        import requests
        from apps.vod.models import Movie, Series, VODLogo

        log = logging.getLogger(LOGGER_NAME)
        dry = bool(settings.get("dry_run", True))
        key = (settings.get("tmdb_api_key") or "").strip()
        st = {"running": True, "action": "fix_images", "dry_run": dry, "started": time.time(), "dead_hosts": [],
              "total": 0, "processed": 0, "fixed": 0, "looked_up": 0, "no_image": 0, "stopped": False, "samples": []}
        self.state.write(st)
        try:
            def titles(model):
                qs = model.objects.all()
                if account is not None:  # after an M3U refresh: only that account's titles
                    qs = qs.filter(m3u_relations__m3u_account_id=account.id).distinct()
                return qs

            if account is not None:
                st["account"] = account.name
            urls_by_host = {}
            for model in (Movie, Series):
                for url in titles(model).filter(logo__isnull=False).values_list("logo__url", flat=True).distinct():
                    urls_by_host.setdefault(urlparse(url or "").netloc, []).append(url)
            dead = self._dead_hosts(urls_by_host, log)
            st["dead_hosts"] = sorted(dead)
            session = requests.Session()
            for model, kind in ((Movie, "movie"), (Series, "tv")):
                rows = [r for r in titles(model).select_related("logo").only("id", "name", "tmdb_id", "logo__url")
                        if r.logo is None or urlparse(r.logo.url or "").netloc in dead]
                st["total"] += len(rows)
                for row in rows:
                    st["processed"] += 1
                    new = None
                    name = (row.logo.url.rstrip("/").rsplit("/", 1)[-1] if row.logo else "")
                    if row.logo and self._TMDB_FILE.match(name):
                        new = self._TMDB_IMG + name
                    elif key and row.tmdb_id:
                        new = self._tmdb_poster(session, key, kind, row.tmdb_id)
                        st["looked_up"] += 1
                    if not new:
                        st["no_image"] += 1
                        continue
                    st["fixed"] += 1
                    if len(st["samples"]) < 10:
                        st["samples"].append(f"{row.name}: {row.logo.url if row.logo else '(none)'}  ->  {new}")
                    if dry:
                        continue
                    logo, _ = VODLogo.objects.get_or_create(url=new, defaults={"name": row.name[:255]})
                    model.objects.filter(pk=row.pk).update(logo=logo)
        finally:
            _release_db_connections()
        st["running"] = False
        st["finished"] = time.time()
        self.state.write(st)
        summary = self._describe(st)
        log.info("Image fix finished: %s", summary.replace("\n", " | "))
        if notify:
            self._notify(summary, False, title="Clean Titles: images fixed" if not dry else "Clean Titles: image fix (dry run)")
        return {"status": "ok", "message": summary}

    @staticmethod
    def _tmdb_poster(session, key, kind, tmdb_id):
        params, headers = {}, {"Accept": "application/json"}
        if key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {key}"
        else:
            params["api_key"] = key
        try:
            r = session.get(f"{TMDB}/{kind}/{tmdb_id}", params=params, headers=headers, timeout=15)
            path = r.json().get("poster_path") if r.ok else None
        except Exception:  # noqa: BLE001
            return None
        return Plugin._TMDB_IMG + path.lstrip("/") if path else None

    # --- unify series -----------------------------------------------------------------
    # Dispatcharr keys a series by (TMDB/IMDb id, language). Series rows from before it
    # had languages carry language NULL; every newer row (and every new provider, e.g. a
    # second M3U account) uses ''. NULL never matches '', so the same show is listed
    # once per provider. Unify folds each NULL row and its '' twin into ONE row (the
    # oldest id, so existing favourites/links keep working) with language ''. From then
    # on Dispatcharr's own refresh attaches every provider's copy of a show to that row,
    # and the player plays by M3U account priority. Folding uses Dispatcharr's own
    # absorb code; the undo data is kept in the row, so Revert Unify splits them again.

    UNIFY = "unify_undo"

    @staticmethod
    def _identity(row):
        if row.tmdb_id:
            return ("tmdb", row.tmdb_id)
        if row.imdb_id:
            return ("imdb", row.imdb_id)
        return ("name", row.name, row.year)

    def _unify_series(self, settings):
        import logging

        from django.core.serializers.json import DjangoJSONEncoder
        from django.db import IntegrityError, transaction
        from django.db.models import Q
        from apps.vod.models import M3USeriesRelation, Series
        from apps.vod.tasks import _absorb_language_row

        log = logging.getLogger(LOGGER_NAME)
        dry = bool(settings.get("dry_run", True))
        legacy = list(Series.objects.filter(language__isnull=True).only("id", "tmdb_id", "imdb_id", "name", "year"))
        groups = {}
        for row in legacy:
            groups.setdefault(self._identity(row), set()).add(row.id)
        st = {"running": True, "action": "unify_series", "dry_run": dry, "started": time.time(),
              "total": len(groups), "processed": 0, "folded": 0, "converted": 0, "absorbed": 0,
              "failed": 0, "stopped": False, "samples": [], "failed_samples": []}
        self.state.flag(self.state.stop_path, False)
        self.state.write(st)
        fields = [f.attname for f in Series._meta.concrete_fields]
        try:
            for ident, ids in groups.items():
                if self.state.has(self.state.stop_path):
                    st["stopped"] = True
                    break
                st["processed"] += 1
                kind = ident[0]
                if kind == "name":
                    twins = Q(name=ident[1], year=ident[2], tmdb_id__isnull=True, imdb_id__isnull=True)
                else:
                    twins = Q(**{f"{kind}_id": ident[1]})
                rows = list(Series.objects.filter(twins).filter(Q(language__isnull=True) | Q(language="")).order_by("id"))
                if not rows:
                    continue
                keep, donors = rows[0], rows[1:]
                if donors:
                    st["folded"] += 1
                    st["absorbed"] += len(donors)
                    if len(st["samples"]) < 15:
                        accts = sorted({n for r in rows for n in r.m3u_relations.values_list("m3u_account__name", flat=True)})
                        st["samples"].append(f"{keep.name} ({len(rows)} rows -> 1: {', '.join(accts)})")
                else:
                    st["converted"] += 1
                if dry:
                    continue
                try:
                    with transaction.atomic():
                        absorbed = []
                        for donor in donors:
                            snap = json.loads(json.dumps({f: getattr(donor, f) for f in fields}, cls=DjangoJSONEncoder))
                            rel_ids = list(M3USeriesRelation.objects.filter(series_id=donor.id).values_list("id", flat=True))
                            absorbed.append({"fields": snap, "relations": rel_ids})
                            _absorb_language_row(keep, donor, M3USeriesRelation, "series", refetch_episodes=True)
                        keep.refresh_from_db()
                        cp = dict(keep.custom_properties or {}) if isinstance(keep.custom_properties, dict) else {}
                        mark = dict(cp.get(MARK) or {})
                        mark[self.UNIFY] = {"at": int(time.time()), "language": keep.language, "name": keep.name,
                                            "absorbed": absorbed}
                        name = keep.name
                        if donors and mark.get("tag"):
                            # The duplicate tag ("[NF]") made no sense any more: one row now.
                            name = mark.get("base_name") or name
                            mark.update(applied_name=name, tag=None)
                        cp[MARK] = mark
                        Series.objects.filter(pk=keep.pk).update(language="", name=name, custom_properties=cp)
                except IntegrityError as exc:
                    # e.g. its IMDb id is already used by another show's '' row
                    st["failed"] += 1
                    if donors:
                        st["folded"] -= 1
                        st["absorbed"] -= len(donors)
                    else:
                        st["converted"] -= 1
                    if len(st["failed_samples"]) < 10:
                        st["failed_samples"].append(f"{keep.name}: {str(exc).splitlines()[0][:120]}")
                if st["processed"] % 50 == 0:
                    self.state.write(st)
        finally:
            _release_db_connections()
        return self._finish(st, log, "Unify", "Clean Titles: series unified" if not dry else "Clean Titles: unify (dry run)")

    def _revert_unify(self):
        import logging

        from django.db import IntegrityError, transaction
        from apps.vod.models import M3USeriesRelation, Series, VODLogo
        from apps.vod.tasks import _split_series_episodes

        log = logging.getLogger(LOGGER_NAME)
        rows = list(Series.objects.filter(**{f"custom_properties__{MARK}__has_key": self.UNIFY}).values_list("id", flat=True))
        st = {"running": True, "action": "revert_unify", "started": time.time(), "total": len(rows), "processed": 0,
              "reverted": 0, "restored": 0, "gone": 0, "failed": 0, "stopped": False}
        self.state.flag(self.state.stop_path, False)
        self.state.write(st)
        try:
            for pk in rows:
                if self.state.has(self.state.stop_path):
                    st["stopped"] = True
                    break
                st["processed"] += 1
                try:
                    with transaction.atomic():
                        keep = Series.objects.select_for_update().get(pk=pk)
                        cp = dict(keep.custom_properties or {})
                        mark = dict(cp.get(MARK) or {})
                        undo = mark.pop(self.UNIFY)
                        # Free the '' slot first: the restored rows may need it.
                        Series.objects.filter(pk=pk).update(language=undo.get("language"))
                        for item in undo.get("absorbed") or []:
                            rel_ids = list(M3USeriesRelation.objects.filter(id__in=item["relations"]).values_list("id", flat=True))
                            if not rel_ids:
                                st["gone"] += 1  # the provider no longer lists that copy
                                continue
                            f = dict(item["fields"])
                            if f.get("logo_id") and not VODLogo.objects.filter(pk=f["logo_id"]).exists():
                                f["logo_id"] = None
                            if Series.objects.filter(uuid=f.get("uuid")).exists():
                                f.pop("uuid", None)
                            if Series.objects.filter(pk=f["id"]).exists():
                                f.pop("id")
                            donor = Series(**f)
                            donor.save(force_insert=True)
                            Series.objects.filter(pk=donor.pk).update(created_at=f.get("created_at"), updated_at=f.get("updated_at"))
                            M3USeriesRelation.objects.filter(id__in=rel_ids).update(series=donor)
                            st["restored"] += 1
                        _split_series_episodes(keep)  # each episode stream back to its copy's series
                        if not mark:
                            cp.pop(MARK, None)
                        else:
                            cp[MARK] = mark
                        Series.objects.filter(pk=pk).update(custom_properties=cp or None)
                    st["reverted"] += 1
                except IntegrityError as exc:
                    st["failed"] += 1
                    log.warning("Revert unify of series %s failed: %s", pk, exc)
                if st["processed"] % 50 == 0:
                    self.state.write(st)
        finally:
            _release_db_connections()
        return self._finish(st, log, "Revert unify", "Clean Titles: unify reverted")

    def _finish(self, st, log, label, title):
        st["running"] = False
        st["finished"] = time.time()
        self.state.write(st)
        self.state.flag(self.state.stop_path, False)
        summary = self._describe(st)
        log.info("%s finished: %s", label, summary.replace("\n", " | "))
        self._notify(summary, st.get("stopped") or st.get("failed"), title=title)
        return {"status": "ok", "message": summary}

    # --- streaming categories ------------------------------------------------------------
    # Providers spread one service over many categories ("NETFLIX MOVIES", "|EN| NETFLIX",
    # "NETFLIX MOVIES 4K ..."). A title's category is stored on each provider's relation
    # to it, so grouping re-points those relations at one shared category per group.
    # Group categories are linked to the built-in "custom" account, which is never VOD
    # scanned: Dispatcharr's VODs page then lists them, and a refresh's cleanup (which
    # deletes categories its provider no longer lists) never removes them, so their ids
    # stay stable for players. Every VOD scan puts titles back in the provider's category,
    # so grouping also runs after each scan. Only the category reference changes, never
    # streams. The provider category of every moved relation is kept for the revert.

    GROUPS_FILE = "category_groups.json"
    CATCH_ALL = "Other"  # never a category: its titles go to a service or the fallback

    @staticmethod
    def _default(field_id):
        return next((f.get("default") for f in _MANIFEST["fields"] if f["id"] == field_id), None)

    @staticmethod
    def _norm(s):
        import unicodedata

        # NFKC turns the providers' superscript tags ("⁴ᴷ ³⁸⁴⁰ᴾ") into plain "4K 3840P".
        return " ".join(unicodedata.normalize("NFKC", s or "").casefold().split())

    @staticmethod
    def _category_map():
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "category_map.txt")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _parse_groups(self, text):
        """[movies]/[series] sections of 'Group name = pattern, pattern' lines.
        Patterns ignore case and spacing; * matches anything. First matching line wins."""
        rules, errors, section = {"movie": [], "series": []}, [], None
        for n, raw in enumerate((text or "").splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            low = line.lower().replace(" ", "")
            if low in ("[movies]", "[movie]"):
                section = "movie"
                continue
            if low in ("[series]", "[shows]"):
                section = "series"
                continue
            name, _, pats = line.partition("=")
            pats = [self._norm(p).replace("[", "[[]") for p in pats.split(",") if p.strip()]
            if section is None or not name.strip() or not pats:
                errors.append(f"line {n}: {raw.strip()}")
                continue
            rules[section].append((name.strip(), pats))
        return rules, errors

    def _group_of(self, name, rules):
        import fnmatch

        n = self._norm(name)
        for target, pats in rules:
            if any(fnmatch.fnmatchcase(n, p) for p in pats):
                return target
        return None

    def _source_group(self, name, rules, keep):
        """What a provider category sorts as: its line in the map, else the catch-all, so a
        category the map doesn't know yet can't stay as a category. None leaves it alone."""
        target = self._group_of(name, rules) or self.CATCH_ALL
        return None if name in keep or name == target else target

    def _groups_data(self, data=None):
        path = os.path.join(self.state.dir, self.GROUPS_FILE)
        if data is not None:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
            return data
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data.setdefault("created", {})
        data.setdefault("orig", {})
        return data

    _GROUP_TYPES = (("movie", "M3UMovieRelation", "movie_id"), ("series", "M3USeriesRelation", "series_id"))

    PLATFORM_CACHE = "platforms.json"
    PLATFORM_TTL = 30 * 86400  # services change their catalogue: look titles up again after ~a month

    # Last-resort services: only chosen when no other listed service streams the title
    # (Outlander, a Starz original, also streams on Netflix: it goes to Netflix), and titles
    # filed under them by a provider are still looked up. "Premium Cable" is the providers'
    # mixed Showtime/Starz/AMC+/MGM+/OSN+ group; it keeps what TMDB can't place.
    SOFT_PLATFORMS = {"Starz", "AMC+", "MGM+", "Premium Cable"}

    @classmethod
    def _platform_name(cls, group, order):
        """The streaming service a group firmly stands for ('Netflix Kids' -> 'Netflix'), else None."""
        if not group:
            return None
        g = group[:-5].strip() if group.casefold().endswith(" kids") else group
        return g if g in order and g not in cls.SOFT_PLATFORMS else None

    @staticmethod
    def _map_service(name):
        """TMDB network/provider name -> service group. Brand channels sold through Amazon,
        Apple or Roku ('Paramount+ Amazon Channel') count as the brand."""
        n = " ".join((name or "").casefold().split())
        for suffix in (" amazon channel", " apple tv channel", " roku premium channel", " roku channel"):
            if n.endswith(suffix):  # a brand's channel resold by Amazon/Apple/Roku counts as the brand
                n = n[: -len(suffix)].strip()
                break
        if "paramount+" in n or "paramount plus" in n or "showtime" in n:  # not "Paramount Network" (cable)
            return "Paramount+"
        if "peacock" in n:
            return "Peacock"
        if "disney" in n:
            return "Disney+"
        if "hulu" in n:
            return "Hulu"
        if "hbo" in n or n == "max" or n.startswith("max "):
            return "HBO Max"
        if "starz" in n:
            return "Starz"
        if "amc+" in n or "amc plus" in n or n == "amc":  # AMC's originals stream on AMC+
            return "AMC+"
        if "mgm" in n or "epix" in n:  # Epix became MGM+
            return "MGM+"
        if "discovery" in n:
            return "Discovery+"
        if "netflix" in n:
            return "Netflix"
        if n in ("apple tv", "apple tv+", "apple tv plus"):  # renamed from Apple TV+ to Apple TV
            return "Apple TV+"
        if "prime video" in n or n == "amazon" or "mx player" in n:
            return "Prime Video"
        return None

    # The other services TMDB lists, offered in the service list. Variants of one service
    # share a key; live-TV bundles and cable-login channel apps are not catalogues.
    _EXTRA_ALIASES = {
        "hallmarktv": "Hallmark+", "hallmark+": "Hallmark+", "hallmarkplus": "Hallmark+",
        "pureflix": "Great American Pure Flix", "greatamericanpureflix": "Great American Pure Flix",
        "acorntv": "Acorn TV", "britbox": "BritBox", "crunchyroll": "Crunchyroll", "hidive": "HIDIVE",
        "historyvault": "HISTORY Vault", "curiositystream": "CuriosityStream",
        "mzchoice": "MHz Choice", "mhzchoice": "MHz Choice",
        "pbs": "PBS", "pbsmasterpiece": "PBS", "pbsdocumentaries": "PBS", "pbsliving": "PBS", "pbskids": "PBS",
        "thirteen": "PBS", "weta+": "PBS", "kqed": "PBS",
        "plex": "Plex", "plexchannel": "Plex", "tubitv": "Tubi", "tubi": "Tubi", "therokuchannel": "The Roku Channel",
        "fandangoathomefree": "Fandango at Home", "fandangoathome": "Fandango at Home",
        "youtube": "YouTube", "youtubefree": "YouTube", "youtubepremium": "YouTube",
        "justwatchtvplus": "JustWatch TV", "justwatchtvfree": "JustWatch TV", "vix": "ViX", "vixpremium": "ViX",
        "shoutfactory": "Shout! Factory TV", "shoutfactorytv": "Shout! Factory TV", "mubi": "MUBI",
        "allblk": "ALLBLK", "indieflix": "IndieFlix", "indieflixshorts": "IndieFlix", "midnightpulp": "Midnight Pulp",
        "lifetimemovieclub": "Lifetime Movie Club", "cineverse": "Cineverse", "fandor": "Fandor",
        "filmmovementplus": "Film Movement Plus", "kocowa": "Kocowa", "arrow": "ARROW",
    }
    _EXTRA_BLOCK = {
        "fubotv", "philo", "youtubetv", "spectrumondemand", "slingtv", "directv", "directvstream", "xfinitystream",
        "tcm", "history", "ae", "usanetwork", "oxygen", "trutv", "tbs", "tnt", "vh1", "lifetime", "nbc", "abc",
        "cbs", "fox", "adultswim", "bravo", "syfy", "freeform", "fxnow", "comedycentral", "mtv", "bet", "hgtv",
        "tlc", "amc", "vicetv", "foodnetwork", "discoverygo", "nickelodeon", "cartoonnetwork",
    }

    # Pay-per-title stores: TMDB lists them as providers, but only subscription and free
    # listings decide a title's service, so picking one would do nothing.
    _STORES = {"amazonvideo", "appletvstore", "googleplaymovies", "justwatchtvstore", "fandango"}
    SERVICE_CACHE = "region_services.json"
    SERVICE_TTL = 30 * 86400

    def _service_names(self, results, region):
        """Service names from TMDB's provider lists, most popular first. One entry per
        service, named as the order setting needs them."""
        import re

        best = {}
        for row in results:
            raw = row.get("provider_name")
            name = self._map_service(raw) or self._canon(raw)[1]
            key = re.sub(r"[^a-z0-9+]", "", (name or "").casefold())
            if not key or key in self._STORES:
                continue
            rank = (row.get("display_priorities") or {}).get(region, row.get("display_priority", 9999))
            if key not in best or rank < best[key][0]:
                best[key] = (rank, name)
        return [name for _, name in sorted(best.values(), key=lambda item: (item[0], item[1].casefold()))]

    @staticmethod
    def _fetch_services(key, region):
        """TMDB's provider rows for a region, movies and series together."""
        import requests

        params, headers = {"watch_region": region}, {"Accept": "application/json"}
        if key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {key}"
        else:
            params["api_key"] = key
        rows = []
        for kind in ("movie", "tv"):
            reply = requests.get(f"{TMDB}/watch/providers/{kind}", params=params, headers=headers, timeout=5)
            reply.raise_for_status()
            rows += reply.json().get("results") or []
        return rows

    def _region_services(self, settings):
        """The region's services, from TMDB at most once a month per region. Runs while the
        plugin loads, so any failure just leaves the last list, or none."""
        region = (settings.get("platform_region") or self._default("platform_region") or "US").strip().upper()
        path = os.path.join(self.state.dir, self.SERVICE_CACHE)
        try:
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        entry = cache.get(region) or {}
        key = (settings.get("tmdb_api_key") or "").strip()
        if not key or time.time() - entry.get("at", 0) < self.SERVICE_TTL:
            return entry.get("names") or []
        try:
            names = self._service_names(self._fetch_services(key, region), region)
            cache[region] = {"at": time.time(), "names": names}
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"  # loads can overlap
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, path)
            return names
        except Exception as e:  # the message can hold the request URL, and with it the API key
            logging.getLogger(LOGGER_NAME).warning("Could not list streaming services: %s", type(e).__name__)
            return entry.get("names") or []

    def _canon(self, raw):
        """(key, display name) of a service TMDB lists that isn't one of the named ones, else (None, None)."""
        import re

        memo = self.__dict__.setdefault("_canon_memo", {})
        if raw in memo:
            return memo[raw]
        memo[raw] = (None, None)
        if not raw or self._map_service(raw):
            return None, None
        full = re.sub(r"[^a-z0-9+]", "", raw.casefold())
        if full in self._EXTRA_ALIASES:  # e.g. "The Roku Channel" is a service, not a resale suffix
            memo[raw] = (full, self._EXTRA_ALIASES[full])
            return memo[raw]
        name = re.sub(r"\s+(amazon channel|apple tv channel|roku premium channel|roku channel)\s*$", "",
                      " ".join(raw.split()), flags=re.I).strip()
        key = re.sub(r"[^a-z0-9+]", "", name.casefold())
        if not key or key in self._EXTRA_BLOCK:
            return None, None
        memo[raw] = (key, self._EXTRA_ALIASES.get(key, name))
        return memo[raw]

    def _service_key(self, raw):
        """One spelling-proof key per service, so TMDB's names match the selected ones."""
        import re

        memo = self.__dict__.setdefault("_key_memo", {})
        if raw not in memo:
            name = self._map_service(raw) or self._canon(raw)[1] or raw or ""
            memo[raw] = re.sub(r"[^a-z0-9+]", "", name.casefold())
        return memo[raw]

    def _pick_platform(self, networks, flatrate, order, free=()):
        """The selected service a title belongs to, else "". Only selected services count,
        and the first one selected wins, paid or free."""
        nets = [self._map_service(n) for n in networks]
        for p in nets:  # a series' original network decides first (Dexter: Resurrection -> Paramount+)
            if p in order and p not in self.SOFT_PLATFORMS:
                return p
        found = {self._service_key(x) for x in (*flatrate, *free)} | {self._service_key(n) for n in nets if n}
        listed = [p for p in order if self._service_key(p) in found]  # Outlander (Starz) -> Netflix
        return next((p for p in listed if p not in self.SOFT_PLATFORMS), next(iter(listed), ""))

    @staticmethod
    def _watch(session, key, kind, tmdb_id, region, limiter):
        """(networks, subscription services in region) for a title, or None if TMDB has no such title."""
        url = f"{TMDB}/{'movie' if kind == 'movie' else 'tv'}/{tmdb_id}"
        params = {"append_to_response": "watch/providers"}
        headers = {"Accept": "application/json"}
        if key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {key}"
        else:
            params["api_key"] = key
        for attempt in range(4):
            limiter.wait()
            r = session.get(url, params=params, headers=headers, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2)) + attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 401:
                raise PermissionError("TMDB rejected the API key (401).")
            r.raise_for_status()
            d = r.json()
            prov = (((d.get("watch/providers") or {}).get("results") or {}).get(region) or {})
            return ([n.get("name", "") for n in d.get("networks") or []],
                    [x.get("provider_name", "") for x in prov.get("flatrate") or []],
                    [x.get("provider_name", "") for x in (prov.get("free") or []) + (prov.get("ads") or [])])
        raise RuntimeError("TMDB kept rate-limiting (429).")

    def _platforms_for(self, ctype, title_ids, settings, order, st, log):
        """{title id: service} for these titles, from the cache or TMDB."""
        import requests
        from apps.vod.models import Movie, Series

        if not title_ids:
            return {}
        model = Movie if ctype == "movie" else Series
        region = (settings.get("platform_region") or self._default("platform_region") or "US").strip().upper()
        key = (settings.get("tmdb_api_key") or "").strip()
        path = os.path.join(self.state.dir, self.PLATFORM_CACHE)
        try:
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}

        def save():
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(path + ".tmp", path)

        ids = list(title_ids)
        tmdb_of = {}
        for i in range(0, len(ids), 5000):
            tmdb_of.update((pk, t) for pk, t in model.objects.filter(id__in=ids[i:i + 5000])
                           .exclude(tmdb_id__isnull=True).exclude(tmdb_id="").values_list("id", "tmdb_id"))
        now = time.time()
        ck = lambda t: f"{ctype}:{region}:{t}"  # noqa: E731
        for t in set(tmdb_of.values()):  # re-decide from TMDB's stored answer (rules or order may have changed)
            e = cache.get(ck(t))
            if e and len(e) >= 5:
                pass  # re-decided below, once every answer is in
            elif e and (e[0] in self.SOFT_PLATFORMS or not e[0]):
                cache.pop(ck(t))  # no service yet, and TMDB's full answer wasn't kept: ask again

        todo = sorted({t for t in tmdb_of.values()
                       if now - (cache.get(ck(t)) or [None, 0])[1] > self.PLATFORM_TTL + (int(t) % 7 if str(t).isdigit() else 0) * 86400})
        pst = st.setdefault("platform", {"looked_up": 0, "to_look_up": 0, "errors": 0})
        pst["to_look_up"] += len(todo)
        if todo and not key:
            pst["no_key"] = True
            todo = []
        if todo:
            limiter = _RateLimiter(settings.get("requests_per_second") or 20)
            session = requests.Session()
            log.info("Looking up streaming services of %d %s on TMDB", len(todo), ctype)
            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    futures = {pool.submit(self._watch, session, key, ctype, t, region, limiter): t for t in todo}
                    for fut in as_completed(futures):
                        t = futures[fut]
                        pst["looked_up"] += 1
                        try:
                            res = fut.result()
                            res = res or ([], [], [])
                            cache[ck(t)] = [self._pick_platform(res[0], res[1], order, res[2]), time.time(), *res]
                        except PermissionError as exc:
                            pst["error"] = str(exc)
                        except Exception as exc:  # noqa: BLE001 - counted; looked up again next time
                            pst["errors"] += 1
                            if pst["errors"] <= 3:
                                log.warning("Streaming lookup %s %s failed: %s", ctype, t, exc)
                        if pst.get("error") or self.state.has(self.state.stop_path):
                            st["stopped"] = st["stopped"] or self.state.has(self.state.stop_path)
                            for f in futures:
                                f.cancel()
                            break
                        if pst["looked_up"] % 200 == 0:
                            self.state.write(st)
                            save()
            finally:
                save()
        entries = [cache[ck(t)] for t in set(tmdb_of.values()) if len(cache.get(ck(t)) or []) >= 5]
        for e in entries:
            e[0] = self._pick_platform(e[2], e[3], order, e[4])
        return ({pk: cache[ck(t)][0] for pk, t in tmdb_of.items() if (cache.get(ck(t)) or [""])[0]},
                tmdb_of)

    def _now_playing(self, settings, log):
        """TMDB ids of the movies TMDB lists as now playing in the region's cinemas (cached 12 h)."""
        import requests

        region = (settings.get("platform_region") or self._default("platform_region") or "US").strip().upper()
        path = os.path.join(self.state.dir, "now_playing.json")
        try:
            with open(path, encoding="utf-8") as f:
                c = json.load(f)
        except (OSError, ValueError):
            c = {}
        if c.get("region") == region and time.time() - c.get("at", 0) < 12 * 3600:
            return set(c.get("ids") or [])
        key = (settings.get("tmdb_api_key") or "").strip()
        if not key:
            return set(c.get("ids") or [])
        params, headers = {"region": region}, {"Accept": "application/json"}
        if key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {key}"
        else:
            params["api_key"] = key
        ids, page, pages = set(), 1, 1
        try:
            with requests.Session() as session:
                while page <= min(pages, 40):
                    r = session.get(f"{TMDB}/movie/now_playing", params={**params, "page": page}, headers=headers, timeout=15)
                    if r.status_code == 429:
                        time.sleep(float(r.headers.get("Retry-After", 2)))
                        continue
                    r.raise_for_status()
                    d = r.json()
                    ids.update(str(x["id"]) for x in d.get("results") or [])
                    pages = int(d.get("total_pages") or 1)
                    page += 1
        except Exception as exc:  # noqa: BLE001 - keep the last list
            log.warning("Could not fetch the movies in theaters: %s", exc)
            return set(c.get("ids") or [])
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"region": region, "at": time.time(), "ids": sorted(ids)}, f)
        os.replace(path + ".tmp", path)
        return ids

    def _group_categories(self, settings, notify=True):
        import logging
        from collections import Counter, defaultdict

        from django.db import transaction
        from django.db.models import Count
        from apps.m3u.models import M3UAccount
        from apps.vod import models as vm

        log = logging.getLogger(LOGGER_NAME)
        dry = bool(settings.get("dry_run", True))
        rules, errors = self._parse_groups(self._category_map())
        saved = settings.get("platform_order")
        order = _service_list(self._default("platform_order") if saved is None else saved)
        fallback = lambda k: (settings.get(k) if settings.get(k) is not None else self._default(k) or "").strip()  # noqa: E731
        theaters = fallback("theaters_group")
        unmatched = (settings.get("unmatched_group") or "").strip() or self._default("unmatched_group")
        now_playing = self._now_playing(settings, log) if theaters else set()
        st = {"running": True, "action": "group_categories", "dry_run": dry, "started": time.time(),
              "errors": errors[:5], "types": {}, "stopped": False}
        if not order:
            st.update(running=False, finished=time.time(), off=True)
            self.state.write(st)
            return {"status": "ok", "message": self._describe(st)}
        self.state.flag(self.state.stop_path, False)
        self.state.write(st)
        holder = M3UAccount.objects.filter(locked=True, account_type="STD").order_by("id").first()
        data = self._groups_data()
        try:
            for ctype, rel_name, fk in self._GROUP_TYPES:
                Rel = getattr(vm, rel_name)
                created = {n: i for n, i in data["created"].get(ctype, {}).items()
                           if vm.VODCategory.objects.filter(id=i).exists()}
                orig = {int(k): v for k, v in data["orig"].get(ctype, {}).items()}
                ours = set(created.values())
                # Existing categories used as a group (Dispatcharr's own "Uncategorized"): moved
                # titles are tracked there too, but the category itself is never removed.
                used = {n: i for n, i in data.setdefault("used", {}).get(ctype, {}).items()
                        if vm.VODCategory.objects.filter(id=i).exists()}
                cats = {c.id: c.name for c in vm.VODCategory.objects.filter(category_type=ctype).exclude(id__in=ours)}
                keep = {unmatched, theaters, *order}
                src_target = {}
                for cid, cname in cats.items():
                    t = self._source_group(cname, rules[ctype], keep)
                    if t:
                        src_target[cid] = t

                def target_id(t):
                    if t in created:
                        return created[t]
                    c = vm.VODCategory.objects.filter(name=t, category_type=ctype).first()
                    if c is not None:
                        used[t] = c.id  # an existing category of that exact name: use it
                        return c.id
                    if dry:
                        return ("new", t)
                    c = vm.VODCategory.objects.create(name=t, category_type=ctype)
                    created[t] = c.id
                    ours.add(c.id)
                    return c.id

                moves, keep_orig, moved_titles = defaultdict(list), {}, set()
                # Every relation that belongs to a group: (id, current category, title, provider category).
                rows = []
                for rid, cur, title in (Rel.objects.filter(category_id__in=ours | set(used.values()))
                                        .values_list("id", "category_id", fk)):
                    o = orig.get(rid)
                    if o is None or o not in cats:
                        if o is not None:
                            keep_orig[rid] = o
                        continue  # provider category unknown or gone: leave it; the next scan resets it
                    rows.append((rid, cur, title, o))
                for src in src_target:
                    rows.extend((rid, src, title, src)
                                for rid, title in Rel.objects.filter(category_id=src).values_list("id", fk))

                # Titles in a genre or catch-all group go to their streaming service: the one a
                # provider already files them under (a service group), else the one TMDB names.
                platform_of = {}
                for _rid, _cur, title, o in rows:
                    p = self._platform_name(src_target.get(o), order)
                    if p:
                        platform_of.setdefault(title, p)
                need = {title for _rid, _cur, title, o in rows
                        if src_target.get(o) and not self._platform_name(src_target[o], order)
                        and title not in platform_of}
                found, tmdb_of = self._platforms_for(ctype, need, settings, order, st, log)
                platform_of.update(found)
                rule_names = {t for t, _ in rules[ctype]}
                sorted_titles, theater_titles, unmatched_titles = set(), set(), set()

                for rid, cur, title, o in rows:
                    t = src_target.get(o)  # None: its rule is gone, back to the provider category
                    if t and not self._platform_name(t, order):
                        p = platform_of.get(title)
                        if p:
                            p = f"{p} Kids" if "kids" in t.casefold() and f"{p} Kids" in rule_names else p
                            if p != t:
                                sorted_titles.add(title)
                        elif theaters and ctype == "movie" and str(tmdb_of.get(title)) in now_playing:
                            p = theaters  # still in cinemas, not streaming anywhere yet
                            theater_titles.add(title)
                        else:
                            p = unmatched  # on none of the selected services (or not on TMDB at all)
                            unmatched_titles.add(title)
                        t = p or t
                    want = target_id(t) if t else o
                    if t:
                        keep_orig[rid] = o
                    if want != cur:
                        moves[want].append(rid)
                        moved_titles.add(title)

                counts = Counter(dict(Rel.objects.exclude(category_id=None).values_list("category_id")
                                      .annotate(n=Count("id"))))
                before = sum(1 for n in counts.values() if n)
                after_counts = Counter(counts)
                cur_of = dict(Rel.objects.filter(id__in=[r for ids in moves.values() for r in ids])
                              .values_list("id", "category_id")) if moves else {}
                for want, ids in moves.items():
                    for rid in ids:
                        after_counts[cur_of.get(rid)] -= 1
                        after_counts[want] += 1
                after = sum(1 for k, n in after_counts.items() if n > 0 and k is not None)

                if not dry:
                    with transaction.atomic():
                        for want, ids in moves.items():
                            for i in range(0, len(ids), 2000):
                                Rel.objects.filter(id__in=ids[i:i + 2000]).update(category_id=want)
                        if holder is not None:
                            for cid in created.values():
                                vm.M3UVODCategoryRelation.objects.get_or_create(
                                    m3u_account=holder, category_id=cid,
                                    defaults={"enabled": True, "custom_properties": {}})
                        # A group left empty (rule removed, or nothing in it now) is dropped.
                        for name, cid in list(created.items()):
                            if not Rel.objects.filter(category_id=cid).exists():
                                vm.VODCategory.objects.filter(id=cid).delete()
                                del created[name]
                    data["created"][ctype] = created
                    data["used"][ctype] = used
                    data["orig"][ctype] = {str(k): v for k, v in keep_orig.items()}
                    self._groups_data(data)
                st["types"][ctype] = {"before": before, "after": after, "titles": len(moved_titles),
                                      "groups": len({t for t in src_target.values()}),
                                      "sorted": len(sorted_titles), "theaters": len(theater_titles),
                                      "unmatched": len(unmatched_titles)}
                self.state.write(st)
        except Exception as exc:  # noqa: BLE001
            log.exception("Streaming categories failed")
            st["failed"] = str(exc)
        finally:
            _release_db_connections()
        st["running"] = False
        st["finished"] = time.time()
        self.state.write(st)
        summary = self._describe(st)
        log.info("Streaming categories finished: %s", summary.replace("\n", " | "))
        if notify:
            self._notify(summary, bool(st.get("failed")),
                         title="Clean Titles: streaming categories applied" if not dry else "Clean Titles: streaming categories (dry run)")
        return {"status": "ok", "message": summary}

    def _revert_groups(self):
        import logging
        from collections import defaultdict

        from django.db import transaction
        from apps.vod import models as vm

        log = logging.getLogger(LOGGER_NAME)
        data = self._groups_data()
        st = {"running": True, "action": "revert_groups", "started": time.time(), "reverted": 0, "left": 0, "stopped": False}
        self.state.write(st)
        try:
            for ctype, rel_name, _fk in self._GROUP_TYPES:
                Rel = getattr(vm, rel_name)
                created = data["created"].get(ctype, {})
                orig = {int(k): v for k, v in data["orig"].get(ctype, {}).items()}
                exists = set(vm.VODCategory.objects.filter(id__in=set(orig.values())).values_list("id", flat=True))
                moves = defaultdict(list)
                used = set(data.get("used", {}).get(ctype, {}).values())
                mine = set(created.values())
                for rid, cid in Rel.objects.filter(category_id__in=mine | used).values_list("id", "category_id"):
                    o = orig.get(rid)
                    if o in exists:
                        moves[o].append(rid)
                    elif cid in mine:  # in a plugin group with no known provider category
                        st["left"] += 1
                    # else: a title Dispatcharr itself put in its Uncategorized: not ours, untouched
                with transaction.atomic():
                    for o, ids in moves.items():
                        for i in range(0, len(ids), 2000):
                            st["reverted"] += Rel.objects.filter(id__in=ids[i:i + 2000]).update(category_id=o)
                    for name, cid in list(created.items()):
                        if not Rel.objects.filter(category_id=cid).exists():
                            vm.VODCategory.objects.filter(id=cid).delete()
                            del created[name]
                data["created"][ctype] = created
                data.setdefault("used", {})[ctype] = {}
                data["orig"][ctype] = {}
                self._groups_data(data)
        except Exception as exc:  # noqa: BLE001
            log.exception("Revert streaming categories failed")
            st["failed"] = str(exc)
        finally:
            _release_db_connections()
        return self._finish(st, log, "Revert streaming categories", "Clean Titles: streaming categories reverted")

    # --- revert -----------------------------------------------------------------

    def _revert_all(self):
        """Revert renames (and tags) to the provider's original names."""
        import logging

        from django.db import IntegrityError, transaction
        from apps.vod.models import Movie, Series

        log = logging.getLogger(LOGGER_NAME)
        st = {"running": True, "action": "revert_all", "started": time.time(), "processed": 0,
              "reverted": 0, "conflicts": 0, "total": 0, "stopped": False}
        self.state.write(st)
        try:
            for model in (Movie, Series):
                rows = model.objects.filter(custom_properties__has_key=MARK).only("id", "name", "custom_properties")
                st["total"] += rows.count()
                for obj in rows.iterator(chunk_size=500):
                    st["processed"] += 1
                    cp = dict(obj.custom_properties or {})
                    mark = cp.pop(MARK, None) or {}
                    if not mark.get("applied_name"):
                        continue
                    original = mark.get("original_name") or obj.name
                    if mark.get(self.UNIFY):
                        cp[MARK] = {self.UNIFY: mark[self.UNIFY]}  # keep: Revert Unify still works
                    try:
                        with transaction.atomic():
                            model.objects.filter(pk=obj.pk).update(name=original, custom_properties=cp or None)
                        st["reverted"] += 1
                    except IntegrityError:
                        st["conflicts"] += 1
                    if st["processed"] % 200 == 0:
                        self.state.write(st)
        finally:
            _release_db_connections()
        st["running"] = False
        st["finished"] = time.time()
        self.state.write(st)
        summary = self._describe(st)
        log.info("Revert finished: %s", summary.replace("\n", " | "))
        self._notify(summary, False, title="Clean Titles: revert done")
        return {"status": "ok", "message": summary}

    # --- reporting ----------------------------------------------------------------

    @staticmethod
    def _describe(st):
        if not st:
            return "No run yet."
        state = "running" if st.get("running") else ("stopped" if st.get("stopped") else "finished")
        if st.get("action") == "waiting":
            if not st.get("running"):
                return f"Stopped while waiting for the VOD scan of {st.get('account')}; it is handled at the next refresh."
            return f"Waiting for the VOD scan of {st.get('account')} to finish, then its new titles get renamed."
        if st.get("action") == "group_categories":
            if st.get("off"):
                return "Streaming categories are off (no streaming services are selected)."
            dry = st.get("dry_run")
            lines = [f"Streaming categories {'dry run ' if dry else ''}{state}."]
            for ctype, label in (("movie", "Movies"), ("series", "Series")):
                t = (st.get("types") or {}).get(ctype)
                if t:
                    lines.append(f"{label}: {t['before']} categories with titles -> {t['after']}"
                                 f" ({t['titles']} titles {'would be' if dry else ''} moved into {t['groups']} groups).")
            srt = sum((t or {}).get("sorted", 0) for t in (st.get("types") or {}).values())
            thr = sum((t or {}).get("theaters", 0) for t in (st.get("types") or {}).values())
            unm = sum((t or {}).get("unmatched", 0) for t in (st.get("types") or {}).values())
            pl = st.get("platform")
            if pl:
                lines.append(f"Streaming services: {srt} titles {'would be' if dry else ''} filed under their service"
                             f" ({pl.get('looked_up', 0)}/{pl.get('to_look_up', 0)} looked up on TMDB"
                             f"{', ' + str(pl['errors']) + ' lookups failed' if pl.get('errors') else ''}).")
                if thr or unm:
                    lines.append(f"{thr} movies in theaters, {unm} titles on no streaming service (Uncategorized).")
                if pl.get("no_key"):
                    lines.append("Set the TMDB API key to look up streaming services of new titles.")
                if pl.get("error"):
                    lines.append(pl["error"])
            if st.get("errors"):
                lines.append("Lines not understood: " + "; ".join(st["errors"]))
            if st.get("failed"):
                lines.append(f"Failed: {st['failed']}")
            return "\n".join(lines).replace("  ", " ")
        if st.get("action") == "revert_groups":
            return (f"Revert streaming categories {state}: {st.get('reverted', 0)} titles back in their provider categories"
                    f"{', ' + str(st['left']) + ' left for the next refresh to reset' if st.get('left') else ''}"
                    f"{'. Failed: ' + st['failed'] if st.get('failed') else ''}.")
        if st.get("action") == "fix_images":
            dry = st.get("dry_run")
            hosts = ", ".join(st.get("dead_hosts") or []) or "none"
            lines = [f"Image fix {'dry run ' if dry else ''}{state}: image servers not answering: {hosts}.",
                     f"{st.get('processed', 0)}/{st.get('total', 0)} titles without a working image:"
                     f" {st.get('fixed', 0)} {'would get' if dry else 'got'} the TMDB poster"
                     f" ({st.get('looked_up', 0)} looked up on TMDB), {st.get('no_image', 0)} have no replacement."]
            if st.get("samples"):
                lines.append("Examples:")
                lines.extend(f"  {x}" for x in st["samples"])
            return "\n".join(lines)
        if st.get("action") == "unify_series":
            dry = st.get("dry_run")
            lines = [f"Unify series {'dry run ' if dry else ''}{state}: {st.get('processed', 0)}/{st.get('total', 0)} legacy shows."
                     f" {st.get('folded', 0)} {'would be' if dry else ''} combined with their other copies"
                     f" ({st.get('absorbed', 0)} duplicate rows {'would be' if dry else ''} folded in),"
                     f" {st.get('converted', 0)} {'would be' if dry else ''} updated with no duplicate, {st.get('failed', 0)} failed."]
            if st.get("samples"):
                lines.append("Examples:")
                lines.extend(f"  {s}" for s in st["samples"])
            if st.get("failed_samples"):
                lines.append("Failed:")
                lines.extend(f"  {s}" for s in st["failed_samples"])
            return "\n".join(lines).replace("  ", " ").replace("\n ", "\n  ")
        if st.get("action") == "revert_unify":
            return (f"Revert unify {state}: {st.get('reverted', 0)}/{st.get('total', 0)} shows split back,"
                    f" {st.get('restored', 0)} copies restored as their own show,"
                    f" {st.get('gone', 0)} not restored (the provider no longer lists them), {st.get('failed', 0)} failed.")
        if st.get("action") == "revert_all":
            return (f"Revert {state}: {st.get('reverted', 0)} restored, {st.get('conflicts', 0)} skipped "
                    f"(name already taken), {st.get('processed', 0)}/{st.get('total', 0)} processed.")
        verb = "would rename" if st.get("dry_run") else "renamed"
        lines = [
            f"{'Dry run' if st.get('dry_run') else 'Rename run'} {state}: {st.get('processed', 0)}/{st.get('total', 0)} looked up. "
            f"{st.get('renamed', 0)} {verb}, {st.get('unchanged', 0)} already correct, "
            f"{st.get('not_found', 0)} not on TMDB, {st.get('conflicts', 0)} skipped (name already taken), "
            f"{st.get('errors', 0)} errors."
        ]
        if st.get("message"):
            lines.append(st["message"])
        if st.get("tags"):
            t = st["tags"]
            lines.append(f"Duplicates: tagged {t['tagged']} titles in {t['groups']} groups sharing a TMDB ID"
                         f" ({t['ambiguous']} could not be told apart).")
        if st.get("samples"):
            lines.append("Examples:")
            lines.extend(f"  {s}" for s in st["samples"])
        return "\n".join(lines)

    def _notify(self, message, warning, title=None):
        import logging

        try:
            from core.models import SystemNotification
            from core.utils import send_websocket_notification

            prefix = "imdb-titles-run-"
            SystemNotification.objects.filter(notification_key__startswith=prefix).delete()
            kind = SystemNotification.NotificationType
            n = SystemNotification.objects.create(
                notification_key=f"{prefix}{int(time.time())}",
                notification_type=kind.WARNING if warning else kind.INFO,
                priority=SystemNotification.Priority.HIGH,
                title=title or f"Clean Titles: run {'stopped' if warning else 'done'}",
                message=message,
                is_active=True,
                admin_only=True,
            )
            send_websocket_notification(n)
        except Exception as exc:  # noqa: BLE001 - a notification must never fail the run
            logging.getLogger(LOGGER_NAME).warning("Could not send the notification: %s", exc)


# --- drag and search ----------------------------------------------------------
# Dispatcharr's multi-select can't be sorted or searched, and plugins have no way to add to
# the settings page. Its page is rendered by this same Python process, so the browser code
# goes in as the page is sent. Nothing on disk changes, and any failure leaves the stock page.
PAGE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drag_and_search.js")


def add_page_script(html, script):
    """The page with the script added at the end of its body."""
    tag = "<script>" + script.replace("</", "<\\/") + "</script>"
    return html.replace("</body>", tag + "</body>", 1)


def _page_script_on():
    from apps.plugins.models import PluginConfig

    config = PluginConfig.objects.filter(key="clean_titles").first()
    return bool(config and config.enabled and (config.settings or {}).get("drag_and_search", True))


def _add_to_page(response):
    try:
        if _page_script_on():
            with open(PAGE_SCRIPT, encoding="utf-8") as handle:
                response.content = add_page_script(response.content.decode("utf-8"), handle.read()).encode("utf-8")
    except Exception:  # noqa: BLE001 - the page must always load
        logging.getLogger(LOGGER_NAME).exception("Could not add drag and search to the page")


def _patch_page():
    """Runs once per process. A reloaded plugin keeps the first patch, which reads the
    setting and the script on every page."""
    from django.views.generic import TemplateView

    if getattr(TemplateView, "clean_titles_patched", False):
        return
    original = TemplateView.render_to_response

    def render_to_response(self, context, **kwargs):
        response = original(self, context, **kwargs)
        if self.template_name == "index.html":
            response.add_post_render_callback(_add_to_page)
        return response

    TemplateView.render_to_response = render_to_response
    TemplateView.clean_titles_patched = True


try:
    _patch_page()
except ImportError:  # outside Dispatcharr, as in the tests
    pass
