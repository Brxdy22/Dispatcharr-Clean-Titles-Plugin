"""Run: python3 -B -m unittest test_plugin (from this folder)."""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import plugin as p


def row(name, priority=50):
    return {"provider_name": name, "display_priorities": {"US": priority}}


class Services(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.plugin = p.Plugin()
        self.plugin.state.dir = folder.name
        self.path = os.path.join(folder.name, p.Plugin.SERVICE_CACHE)

    def names(self, rows):
        return self.plugin._service_names(rows, "US")

    def test_one_name_per_service_most_popular_first(self):
        rows = [row("Tubi TV", 9), row("Netflix", 1), row("Netflix Standard with Ads", 4), row("Paramount+ Amazon Channel", 3)]
        self.assertEqual(self.names(rows), ["Netflix", "Paramount+", "Tubi"])

    def test_spelling_variants_merge(self):
        self.assertEqual(self.names([row("BroadwayHD", 1), row("Broadway HD", 2)]), ["BroadwayHD"])

    def test_stores_and_bundles_are_left_out(self):
        rows = [row("Amazon Video"), row("Apple TV Store"), row("Google Play Movies"), row("fuboTV"), row("Hoopla")]
        self.assertEqual(self.names(rows), ["Hoopla"])

    def test_priority_is_per_region(self):
        rows = [{"provider_name": "Hoopla", "display_priorities": {"US": 2, "MX": 90}},
                {"provider_name": "Kanopy", "display_priorities": {"US": 80, "MX": 1}}]
        self.assertEqual(self.plugin._service_names(rows, "MX"), ["Kanopy", "Hoopla"])

    def test_fetched_once_per_region(self):
        with mock.patch.object(p.Plugin, "_fetch_services", return_value=[row("Hoopla")]) as fetch:
            for _ in range(2):
                self.assertEqual(self.plugin._region_services({"tmdb_api_key": "k", "platform_region": "us"}), ["Hoopla"])
            self.assertEqual(fetch.call_count, 1)
            self.plugin._region_services({"tmdb_api_key": "k", "platform_region": "CA"})
            self.assertEqual(fetch.call_count, 2)

    def test_overlapping_loads_both_get_the_list(self):
        barrier = threading.Barrier(4)

        def fetch(_key, _region):
            barrier.wait(5)
            return [row("Hoopla")]

        results = []
        with mock.patch.object(p.Plugin, "_fetch_services", side_effect=fetch):
            threads = [threading.Thread(target=lambda: results.append(self.plugin._region_services({"tmdb_api_key": "k"})))
                       for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results, [["Hoopla"]] * 4)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [p.Plugin.SERVICE_CACHE])

    def test_old_list_is_refreshed(self):
        with open(self.path, "w") as f:
            json.dump({"US": {"at": time.time() - p.Plugin.SERVICE_TTL - 1, "names": ["Old"]}}, f)
        with mock.patch.object(p.Plugin, "_fetch_services", return_value=[row("Hoopla")]):
            self.assertEqual(self.plugin._region_services({"tmdb_api_key": "k"}), ["Hoopla"])

    def test_failure_keeps_the_last_list_and_hides_the_key(self):
        with open(self.path, "w") as f:
            json.dump({"US": {"at": 0, "names": ["Old"]}}, f)
        error = RuntimeError("https://x?api_key=secret")
        with mock.patch.object(p.Plugin, "_fetch_services", side_effect=error):
            with self.assertLogs(p.LOGGER_NAME, "WARNING") as logs:
                self.assertEqual(self.plugin._region_services({"tmdb_api_key": "k"}), ["Old"])
        self.assertNotIn("secret", "".join(logs.output))

    def test_no_key_never_calls_tmdb(self):
        with mock.patch.object(p.Plugin, "_fetch_services") as fetch:
            self.assertEqual(self.plugin._region_services({}), [])
        fetch.assert_not_called()

    def fields(self, settings):
        with mock.patch.object(p.Plugin, "_saved_settings", return_value=settings), \
                mock.patch.object(p.Plugin, "_fetch_services", return_value=[row("Hoopla", 1)]):
            return {f["id"]: f for f in self.plugin.fields}

    def test_defaults_are_always_offered(self):
        field = self.fields({"tmdb_api_key": "k"})["platform_order"]
        values = [o["value"] for o in field["options"]]
        self.assertEqual(values[0], "Hoopla")
        self.assertTrue(set(field["default"]) <= set(values))

    def test_saved_services_stay_offered(self):
        field = self.fields({"platform_order": ["Gone", "Netflix"]})["platform_order"]
        self.assertIn("Gone", [o["value"] for o in field["options"]])

    def test_saved_language_and_region_stay_offered(self):
        fields = self.fields({"language": "xx-XX", "platform_region": "ZZ"})
        self.assertEqual(fields["language"]["options"][-1]["value"], "xx-XX")
        self.assertEqual(fields["platform_region"]["options"][-1]["value"], "ZZ")

    def test_lists_failing_keeps_the_plugin_loading(self):
        with mock.patch.object(p.Plugin, "_saved_settings", side_effect=RuntimeError):
            fields = {f["id"]: f for f in self.plugin.fields}
        self.assertEqual(len(fields["platform_order"]["options"]), len(fields["platform_order"]["default"]))


class Picking(unittest.TestCase):
    ORDER = ["Netflix", "Tubi", "Hulu", "Starz"]

    def pick(self, flatrate=(), free=(), networks=(), order=None):
        return p.Plugin()._pick_platform(list(networks), list(flatrate), order or self.ORDER, list(free))

    def test_unselected_services_never_win(self):
        self.assertEqual(self.pick(["Kanopy"], ["Plex"]), "")
        self.assertEqual(self.pick(["Netflix"], order=["Hulu"]), "")

    def test_first_selected_wins(self):
        self.assertEqual(self.pick(["Hulu", "Netflix"]), "Netflix")
        self.assertEqual(self.pick(["Netflix", "Hulu"], order=["Hulu", "Netflix"]), "Hulu")

    def test_free_services_follow_the_order_too(self):
        self.assertEqual(self.pick(["Hulu"], ["Tubi TV"]), "Tubi")
        self.assertEqual(self.pick(["Netflix"], ["Tubi TV"], order=["Tubi", "Netflix"]), "Tubi")

    def test_extra_can_beat_a_named_service(self):
        self.assertEqual(self.pick(["Netflix", "Hoopla"], order=["Hoopla", "Netflix"]), "Hoopla")

    def test_spelling_variants_match(self):
        self.assertEqual(self.pick(["Broadway HD"], order=["BroadwayHD"]), "BroadwayHD")

    def test_resold_channels_count_as_the_brand(self):
        self.assertEqual(self.pick(["Starz Amazon Channel"]), "Starz")

    def test_original_network_decides_first(self):
        self.assertEqual(self.pick(["Netflix"], networks=["Hulu"]), "Hulu")

    def test_last_resort_only_without_another(self):
        self.assertEqual(self.pick(["Starz", "Hulu"], order=["Starz", "Hulu"]), "Hulu")
        self.assertEqual(self.pick(["Starz"]), "Starz")


class CategoryMap(unittest.TestCase):
    def setUp(self):
        self.plugin = p.Plugin()
        self.rules, self.errors = self.plugin._parse_groups(self.plugin._category_map())

    def source(self, name, kind="movie", keep=("Uncategorized", "In Theaters", "Netflix")):
        return self.plugin._source_group(name, self.rules[kind], set(keep))

    def test_map_parses(self):
        self.assertEqual(self.errors, [])
        self.assertTrue(self.rules["movie"] and self.rules["series"])

    def test_provider_categories_keep_their_service(self):
        self.assertEqual(self.source("NETFLIX MOVIES 2024"), "Netflix")
        self.assertEqual(self.source("NETFLIX KIDS"), "Netflix Kids")

    def test_kids_stay_recognisable(self):
        self.assertIn("kids", self.source("EN - KIDS").casefold())

    def test_unknown_category_is_sorted_too(self):
        self.assertEqual(self.source("BRAND NEW PROVIDER CATEGORY"), p.Plugin.CATCH_ALL)

    def test_fallback_and_selected_categories_stay(self):
        for name in ("Uncategorized", "In Theaters", "Netflix"):
            self.assertIsNone(self.source(name))

    def test_catch_all_is_never_a_service(self):
        self.assertIsNone(p.Plugin._platform_name(p.Plugin.CATCH_ALL, ["Netflix"]))

    def test_removed_settings_are_gone(self):
        ids = {f["id"] for f in p._MANIFEST["fields"]}
        self.assertFalse(ids & {"category_groups", "sort_by_platform", "extra_service_min_titles"})


class RentBuy(unittest.TestCase):
    def test_setting_is_on_by_default(self):
        self.assertEqual(p.Plugin._default("rent_buy_group"), "Rent/Buy")

    def test_store_listings_never_pick_a_service(self):
        self.assertEqual(p.Plugin()._pick_platform([], [], ["Prime Video"], []), "")

    def test_a_teen_rating_without_a_kids_genre_is_not_for_kids(self):
        mature = p.Plugin._is_mature
        tv = lambda *r, genres=(): {"genres": [{"id": g} for g in genres],  # noqa: E731
                                    "content_ratings": {"results": [{"iso_3166_1": "US", "rating": x} for x in r]}}
        self.assertTrue(mature("series", tv("TV-14", genres=[16])))
        self.assertFalse(mature("series", tv("TV-14", genres=[10762])))
        self.assertFalse(mature("series", tv("TV-Y7")))
        self.assertFalse(mature("series", tv()))
        movie = {"genres": [], "release_dates": {"results": [
            {"iso_3166_1": "US", "release_dates": [{"certification": "R"}, {"certification": ""}]},
            {"iso_3166_1": "DE", "release_dates": [{"certification": "PG"}]}]}}
        self.assertTrue(mature("movie", movie))

    def test_teen_ratings_in_three_countries_outweigh_a_family_label(self):
        mature = p.Plugin._is_mature
        rated = lambda *r: {"genres": [{"id": 10751}], "content_ratings": {"results": [  # noqa: E731
            {"iso_3166_1": c, "rating": x} for c, x in r]}}
        self.assertTrue(mature("series", rated(("US", "TV-PG"), ("GB", "12"), ("DE", "12"), ("FR", "12"))))
        self.assertFalse(mature("series", rated(("US", "TV-PG"), ("GB", "12"), ("DE", "12"), ("FR", "0"))))

    def test_free_with_ads_on_prime_video_beats_the_order(self):
        free = ["Amazon Prime Video Free with Ads", "Tubi TV"]
        order = ["Netflix", "Prime Video", "Tubi"]
        self.assertEqual(p.Plugin()._pick_platform([], ["Netflix", "Amazon Prime Video"], order, free), "Prime Video")

    def test_the_prime_ad_plan_and_other_free_services_keep_the_order(self):
        order = ["Netflix", "Prime Video", "Tubi"]
        plugin = p.Plugin()
        self.assertEqual(plugin._pick_platform([], ["Netflix", "Amazon Prime Video with Ads"], order, []), "Netflix")
        self.assertEqual(plugin._pick_platform([], ["Netflix"], order, ["Tubi TV"]), "Netflix")

    def test_free_prime_needs_prime_selected_and_loses_to_the_network(self):
        free = ["Amazon Prime Video Free with Ads"]
        self.assertEqual(p.Plugin()._pick_platform([], ["Netflix"], ["Netflix"], free), "Netflix")
        self.assertEqual(p.Plugin()._pick_platform(["Netflix"], [], ["Prime Video", "Netflix"], free), "Netflix")

    def test_only_store_listings_count(self):
        only = p.Plugin._store_only
        self.assertTrue(only(["", 0, [], [], [], ["Amazon Video"]]))
        self.assertFalse(only(["", 0, [], ["Hoopla"], [], ["Amazon Video"]]))
        self.assertFalse(only(["", 0, [], [], ["Tubi TV"], ["Amazon Video"]]))
        self.assertFalse(only(["", 0, [], [], [], []]))
        self.assertFalse(only(["", 0, [], [], []]))
        self.assertFalse(only(None))

    def test_watch_returns_rent_and_buy(self):
        reply = mock.Mock(status_code=200)
        reply.json.return_value = {"watch/providers": {"results": {"US": {
            "rent": [{"provider_name": "Amazon Video"}], "buy": [{"provider_name": "Amazon Video"}, {"provider_name": "Fandango At Home"}]}}}}
        session = mock.Mock()
        session.get.return_value = reply
        res = p.Plugin._watch(session, "k", "movie", 1, "US", mock.Mock())
        self.assertEqual(res, ([], [], [], ["Amazon Video", "Fandango At Home"]))


class Language(unittest.TestCase):
    def verdict(self, info, keep_anime=True):
        return p.Plugin._language_verdict(info, "en", keep_anime)

    def test_original_or_spoken_english_stays(self):
        self.assertEqual(self.verdict(["en", [], False, False]), "keep")
        self.assertEqual(self.verdict(["de", ["de", "en"], False, False]), "keep")
        self.assertEqual(self.verdict(["ko", ["en", "ko"], False, False]), "keep")  # Squid Game

    def test_other_languages_go(self):
        self.assertEqual(self.verdict(["fr", ["fr"], False, False]), "remove")

    def test_unknown_always_stays(self):
        for info in (None, ["", [], False, False], ["xx", [], False, False]):
            self.assertEqual(self.verdict(info), "unknown")

    def test_anime_stays_when_asked(self):
        self.assertEqual(self.verdict(["ja", ["ja"], True, False]), "anime")
        self.assertEqual(self.verdict(["zh", ["zh"], True, True]), "anime")
        self.assertEqual(self.verdict(["ja", ["ja"], True, False], keep_anime=False), "remove")

    def test_japanese_live_action_is_not_anime(self):
        self.assertEqual(self.verdict(["ja", ["ja"], False, False]), "remove")

    def test_korean_animation_off_crunchyroll_goes(self):
        self.assertEqual(self.verdict(["ko", ["ko"], True, False]), "remove")

    def test_anime_categories_match(self):
        for name in ("EN - MANGA/ANIME", "|EN| ANIME", "NETFLIX ANIMI", "CRUNCHYROLL 4K", "Crunchyroll", "Anime & Animation"):
            self.assertTrue(p.Plugin._ANIME_CATEGORY.search(name), name)
        for name in ("DREAMWORKS ANIMATION", "Netflix", "|EN| CLASSIC CARTOONS"):
            self.assertFalse(p.Plugin._ANIME_CATEGORY.search(name), name)

    def test_filter_is_off_by_default(self):
        self.assertIs(p.Plugin._default("language_filter"), False)

    def test_language_info_reads_any_region(self):
        reply = mock.Mock(status_code=200)
        reply.json.return_value = {"original_language": "ja", "spoken_languages": [{"iso_639_1": "ja"}], "genres": [{"id": 16}],
                                   "watch/providers": {"results": {"JP": {"link": "x", "flatrate": [{"provider_name": "Crunchyroll"}]}}}}
        session = mock.Mock()
        session.get.return_value = reply
        self.assertEqual(p.Plugin._language_info(session, "k", "tv", 1, mock.Mock()), ["ja", ["ja"], True, True])

    def test_missing_title_is_unknown(self):
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=404)
        info = p.Plugin._language_info(session, "k", "movie", "/1", mock.Mock())
        self.assertEqual(self.verdict(info), "unknown")
        self.assertTrue(session.get.call_args[0][0].endswith("/movie/1"))

    def test_preview_button_needs_no_switch_but_remove_does(self):
        plug = p.Plugin()
        with tempfile.TemporaryDirectory() as folder:
            plug.state.dir = folder
            plug.state.status_path = os.path.join(folder, "status.json")
            with mock.patch.object(p.Plugin, "_language_filter") as run, mock.patch.object(p, "_release_db_connections"):
                out = plug.run("language_remove", {}, {"settings": {"dry_run": False}})
                self.assertEqual(out["status"], "error")
                run.assert_not_called()
                plug.run("language_preview", {}, {"settings": {"language_filter": True, "dry_run": False}})
                run.assert_called_once_with({"language_filter": True, "dry_run": False})


class OrderSetting(unittest.TestCase):
    def test_default_order_is_unchanged(self):
        field = next(f for f in p._MANIFEST["fields"] if f["id"] == "platform_order")
        old = "Netflix, Disney+, Prime Video, HBO Max, Hulu, Apple TV+, Paramount+, Peacock, Discovery+, Starz, AMC+, MGM+"
        self.assertEqual(field["default"], p._service_list(old))

    def test_text_and_lists_read_alike(self):
        self.assertEqual(p._service_list("A, B,,"), ["A", "B"])
        self.assertEqual(p._service_list([" A ", "B"]), ["A", "B"])
        self.assertEqual(p._service_list(None), [])



class PageScript(unittest.TestCase):
    def test_goes_at_the_end_of_the_body(self):
        html = p.add_page_script("<html><body><div></div></body></html>", "go();")
        self.assertEqual(html, "<html><body><div></div><script>go();</script></body></html>")

    def test_script_cannot_close_its_own_tag(self):
        self.assertNotIn("</script>x", p.add_page_script("<body></body>", "a('</script>x');"))

    def test_page_without_a_body_is_left_alone(self):
        self.assertEqual(p.add_page_script("<p></p>", "go();"), "<p></p>")

    def test_failure_leaves_the_page_alone(self):
        response = mock.Mock(content=b"<body></body>")
        with mock.patch.object(p, "_page_script_on", side_effect=RuntimeError):
            p._add_to_page(response)
        self.assertEqual(response.content, b"<body></body>")

    def test_off_leaves_the_page_alone(self):
        response = mock.Mock(content=b"<body></body>")
        with mock.patch.object(p, "_page_script_on", return_value=False):
            p._add_to_page(response)
        self.assertEqual(response.content, b"<body></body>")

    def test_on_adds_the_shipped_script(self):
        response = mock.Mock(content=b"<body></body>")
        with mock.patch.object(p, "_page_script_on", return_value=True):
            p._add_to_page(response)
        self.assertIn(b"cleanTitlesDrag", response.content)

    def test_opening_the_list_waits_for_react(self):
        with open(p.PAGE_SCRIPT, encoding="utf-8") as handle:
            script = handle.read()
        self.assertLess(script.index("setTimeout(() => {\n      if (!isOpen(input)) input.click();"), script.index("Enter picks the first"))

    def test_empty_backspace_stays_away_from_mantine(self):
        with open(p.PAGE_SCRIPT, encoding="utf-8") as handle:
            self.assertIn("event.key === 'Backspace' && !input.value", handle.read())

    def test_script_labels_match_the_field(self):
        label = next(f["label"] for f in p._MANIFEST["fields"] if f["id"] == "platform_order")
        with open(p.PAGE_SCRIPT, encoding="utf-8") as handle:
            self.assertIn(label, handle.read())


if __name__ == "__main__":
    unittest.main()
