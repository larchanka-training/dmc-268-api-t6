"""Acceptance criteria of #601 that code can prove, on fake GitHub, Telegram and clock."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import API, CHAT, UI, Clock, FakeGitHub, FakeTelegram, make_settings
from harness import Harness

from review_bot.config import Member
from review_bot.messages import TELEGRAM_MAX_MESSAGE_LEN
from review_bot.ports import Update
from review_bot.service import Bot
from review_bot.store import Store

ALICE, BOB, CAROL = "Alice-Example", "bob-example", "carol-example"
OUTSIDER = "dave-example"


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    db_path = str(tmp_path / "bot.sqlite3")
    github, telegram, clock = FakeGitHub(), FakeTelegram(), Clock()
    bot = Bot(make_settings(db_path), Store(db_path), github, telegram, clock)
    harness = Harness(github, telegram, clock, db_path, bot)
    yield harness
    harness.bot.store.close()


def run(h: Harness, minutes: int) -> None:
    """Poll once a minute for that many minutes."""
    for _ in range(minutes):
        h.clock.advance(minutes=1)
        h.tick()


# AC1-AC3: assignment


def test_pr_without_reviewers_gets_a_reviewer_on_the_next_poll_and_never_its_author(
    h: Harness,
) -> None:
    h.github.open(API, 1, author=ALICE)
    h.tick()
    assert len(h.github.requested_log) == 1
    _, _, login = h.github.requested_log[0]
    assert login != ALICE


def test_a_draft_is_assigned_once_it_is_ready(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, draft=True)
    h.tick()
    assert h.github.requested_log == []
    h.github.update(API, 1, draft=False)
    run(h, 1)
    assert [n for _, n, _ in h.github.requested_log] == [1]


def test_two_prs_in_a_row_go_to_two_different_free_people(tmp_path: Path) -> None:
    db = str(tmp_path / "two.sqlite3")
    team = (Member(ALICE, "alice_tg"), Member(BOB, "bob_tg"))
    github = FakeGitHub()
    store = Store(db)
    try:
        bot = Bot(make_settings(db, team), store, github, FakeTelegram(), Clock())
        github.open(API, 1, author=OUTSIDER)
        github.open(UI, 2, author=OUTSIDER)
        bot.tick()
        assert {login for _, _, login in github.requested_log} == {ALICE, BOB}
    finally:
        store.close()


def test_two_prs_in_separate_polls_also_go_to_different_people(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER)
    h.tick()
    h.github.submit_review(API, 1, h.github.requested_log[0][2])  # load back to zero
    h.github.open(API, 2, author=OUTSIDER)
    run(h, 1)
    first, second = (login for _, _, login in h.github.requested_log)
    assert first != second


def test_the_least_loaded_member_is_chosen_across_both_repos(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(ALICE,))
    h.github.open(UI, 2, author=OUTSIDER, reviewers=(ALICE, CAROL))
    h.github.open(UI, 3, author=OUTSIDER)
    h.tick()
    assert h.github.requested_log == [(UI, 3, BOB)]


def test_a_refused_reviewer_is_skipped_for_the_next_candidate(h: Harness) -> None:
    h.github.rejected.add(ALICE)
    h.github.open(API, 1, author=BOB)
    h.tick()
    assert h.github.requested_log == [(API, 1, CAROL)]


def test_a_pr_with_a_reviewer_is_not_reassigned_even_after_the_review(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(OUTSIDER + "2",))
    h.tick()
    h.github.submit_review(API, 1, OUTSIDER + "2")
    run(h, 3)
    h.github.open(API, 2, author=OUTSIDER)
    run(h, 1)
    h.github.submit_review(API, 2, h.github.requested_log[0][2])
    run(h, 3)
    assert [n for _, n, _ in h.github.requested_log] == [2]


def test_a_pr_reviewed_before_the_bot_saw_it_is_not_assigned(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER)
    h.github.submit_review(API, 1, BOB)
    h.tick()
    assert h.github.requested_log == []


def test_pr_with_requested_teams_is_not_assigned(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, teams=("backend-reviewers",))
    h.tick()
    assert h.github.requested_log == []


def test_a_manually_added_reviewer_shows_up_and_a_removed_one_drops_out(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(ALICE,))
    h.github.open(API, 2, author=OUTSIDER, reviewers=(ALICE, BOB))
    h.tick()
    h.github.add_reviewer(API, 1, CAROL)
    h.github.remove_reviewer(API, 2, ALICE)
    run(h, 120)  # two-hour rule sends the batch
    (text,) = h.telegram.texts
    assert "@carol_tg\n• " in text and "api#1 " in text.split("@carol_tg")[1]
    assert "@bob_tg\n• " in text and "api#2 " in text.split("@bob_tg")[1]
    assert "api#2" not in text.split("@bob_tg")[0]
    assert h.github.requested_log == []


def test_pr_whose_only_reviewer_is_removed_without_review_gets_a_new_reviewer(
    h: Harness,
) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(ALICE,))
    h.tick()
    assert h.github.requested_log == []
    h.github.remove_reviewer(API, 1, ALICE)
    run(h, 1)
    assert len(h.github.requested_log) == 1
    assert h.github.requested_log[0][:2] == (API, 1)
    assert h.github.requested_log[0][2] not in (ALICE, OUTSIDER)


# AC4: re-request after a review


def test_a_re_request_after_a_review_queues_the_pr_again_as_round_2(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,), labels=frozenset({"urgent"}))
    h.tick()
    assert "раунд" not in h.telegram.texts[0]
    h.github.submit_review(API, 1, BOB)
    run(h, 5)
    h.github.add_reviewer(API, 1, BOB)
    run(h, 1)
    assert len(h.telegram.texts) == 2
    assert "api#1 Change 1</a> · dave-example · urgent · раунд 2" in h.telegram.texts[1]
    assert h.github.requested_log == []


def test_removed_and_added_back_without_a_review_is_the_same_round_and_not_repeated(
    h: Harness,
) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(ALICE, BOB), labels=frozenset({"urgent"}))
    h.tick()
    h.github.remove_reviewer(API, 1, BOB)
    run(h, 1)
    h.github.add_reviewer(API, 1, BOB)
    run(h, 180)
    assert len(h.telegram.texts) == 1


# AC5: queue rules


def test_a_single_pr_younger_than_two_hours_is_not_sent(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.tick()
    run(h, 119)
    assert h.telegram.texts == []
    run(h, 1)
    assert len(h.telegram.texts) == 1


def test_ten_prs_less_than_30_minutes_apart_give_one_message(h: Harness) -> None:
    for number in range(1, 11):
        h.github.open(API, number, author=OUTSIDER, reviewers=(BOB,))
        run(h, 10)
    run(h, 30)
    assert len(h.telegram.texts) == 1
    assert h.telegram.texts[0].startswith("🔎 Ревью ждут 10 PR")
    run(h, 300)
    assert len(h.telegram.texts) == 1


def test_two_prs_go_out_after_30_quiet_minutes(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.github.open(API, 2, author=OUTSIDER, reviewers=(CAROL,))
    h.tick()
    run(h, 29)
    assert h.telegram.texts == []
    run(h, 1)
    assert len(h.telegram.texts) == 1


def test_a_steady_stream_is_sent_no_later_than_two_hours_after_the_first_pr(h: Harness) -> None:
    first_seen: datetime | None = None
    sent_at: list[datetime] = []
    for minute in range(0, 300):
        if minute % 20 == 0:
            h.github.open(API, minute + 1, author=OUTSIDER, reviewers=(BOB,))
        before = len(h.telegram.texts)
        h.tick()
        first_seen = first_seen or h.clock.now
        if len(h.telegram.texts) > before:
            sent_at.append(h.clock.now)
        h.clock.advance(minutes=1)
    assert first_seen is not None and sent_at
    assert (sent_at[0] - first_seen).total_seconds() <= 2 * 3600


def test_urgent_goes_out_on_the_next_poll(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.tick()
    assert h.telegram.texts == []
    h.github.update(API, 1, labels=frozenset({"urgent"}))
    run(h, 1)
    assert len(h.telegram.texts) == 1
    assert "• 🔥 <a" in h.telegram.texts[0]


# AC6: never twice in one round, also after a restart


def test_nothing_is_sent_twice_after_a_restart(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.github.open(UI, 2, author=OUTSIDER, reviewers=(BOB,))
    h.tick()
    run(h, 30)
    assert len(h.telegram.texts) == 1
    h.restart()
    run(h, 240)
    assert len(h.telegram.texts) == 1


def test_a_queue_survives_a_restart(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.tick()
    run(h, 60)
    h.restart()
    run(h, 59)
    assert h.telegram.texts == []
    run(h, 1)
    assert len(h.telegram.texts) == 1


def test_a_refused_message_is_retried_and_sent_once(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,), labels=frozenset({"urgent"}))
    h.telegram.reject_next = 1
    h.tick()
    assert h.telegram.texts == []
    run(h, 10)
    assert len(h.telegram.texts) == 1


def test_telegram_transport_error_keeps_notified_mark_without_retry(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,), labels=frozenset({"urgent"}))
    h.telegram.fail_next = 1
    h.tick()
    assert h.telegram.texts == []
    run(h, 180)
    assert h.telegram.texts == []


def test_rejected_digest_does_not_requeue_already_notified_batch_prs(h: Harness) -> None:
    at_berlin(h, 15, 0)
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,), labels=frozenset({"urgent"}))
    h.tick()
    assert len(h.telegram.texts) == 1
    at_berlin(h, 18, 0)
    h.telegram.reject_next = 1
    h.tick()
    assert len(h.telegram.texts) == 1
    at_berlin(h, 18, 1)
    h.tick()
    assert len(h.telegram.texts) == 2
    assert h.telegram.texts[1].startswith("🌙 Висит без ревью к 18:00\n\n")


def test_large_queue_of_60_prs_splits_within_limit_and_tracks_partial_delivery(
    h: Harness,
) -> None:
    for number in range(1, 61):
        h.github.open(
            API,
            number,
            author=OUTSIDER,
            title=f"Long pull request title padding for limit testing #{number:02d}",
            reviewers=(BOB,),
        )
    h.tick()
    run(h, 30)
    assert len(h.telegram.texts) >= 2
    assert all(len(text) <= TELEGRAM_MAX_MESSAGE_LEN for text in h.telegram.texts)
    delivered = "\n".join(h.telegram.texts)
    for number in range(1, 61):
        assert f"api#{number} " in delivered
    count_before_quiet = len(h.telegram.texts)
    run(h, 180)
    assert len(h.telegram.texts) == count_before_quiet


def test_large_queue_partial_chunk_rejection_retries_only_undelivered_chunk(
    h: Harness,
) -> None:
    for number in range(1, 61):
        h.github.open(
            API,
            number,
            author=OUTSIDER,
            title=f"Long pull request title padding for limit testing #{number:02d}",
            reviewers=(BOB,),
        )
    h.tick()
    h.telegram.reject_after = 1
    run(h, 30)
    first_chunk_texts = list(h.telegram.texts)
    assert len(first_chunk_texts) == 1
    h.telegram.reject_after = None
    run(h, 1)
    assert len(h.telegram.texts) == 2
    assert "api#1 " not in h.telegram.texts[1]
    assert "api#60 " in h.telegram.texts[1]


def test_digest_partial_chunk_rejection_marks_date_and_does_not_repeat_first_chunk(
    h: Harness,
) -> None:
    at_berlin(h, 17, 55)
    for number in range(1, 61):
        h.github.open(
            API,
            number,
            author=OUTSIDER,
            title=f"Long digest pull request padding for limit testing #{number:02d}",
            reviewers=(BOB,),
        )
    h.tick()
    assert h.telegram.texts == []
    at_berlin(h, 18, 0)
    h.telegram.reject_after = 1
    h.tick()
    assert len(h.telegram.texts) == 1
    assert h.telegram.texts[0].startswith("🌙 Висит без ревью к 18:00\n\n")
    h.telegram.reject_after = None
    for minute in range(1, 6):
        at_berlin(h, 18, minute)
        h.tick()
    assert len(h.telegram.texts) == 1


def test_a_failed_poll_changes_nothing(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.github.open(UI, 2, author=OUTSIDER, reviewers=(BOB,))
    h.tick()
    h.github.fail_list = True
    run(h, 30)
    h.github.fail_list = False
    run(h, 1)
    assert len(h.telegram.texts) == 1
    assert "api#1" in h.telegram.texts[0] and "ui#2" in h.telegram.texts[0]


# AC7: closed, merged, draft or reviewed PRs drop out


def test_closed_draft_and_reviewed_prs_are_not_in_the_message(h: Harness) -> None:
    for number in (1, 2, 3, 4):
        h.github.open(API, number, author=OUTSIDER, reviewers=(BOB,))
    h.tick()
    h.github.close(API, 1)  # closed or merged: gone from the open list
    h.github.update(API, 2, draft=True)
    h.github.submit_review(API, 3, BOB)
    run(h, 1)
    h.github.update(API, 4, labels=frozenset({"urgent"}))
    run(h, 1)
    (text,) = h.telegram.texts
    assert "api#4" in text
    for gone in ("api#1 ", "api#2 ", "api#3 "):
        assert gone not in text
    run(h, 240)
    assert len(h.telegram.texts) == 1


# AC8: message format


def test_the_message_matches_the_example_format(h: Harness) -> None:
    h.github.open(API, 562, author="axyi", title="Demo script v1.0", reviewers=(ALICE,))
    h.github.open(UI, 322, author="axyi", title="RAG <excerpts>", reviewers=(ALICE,))
    h.github.open(API, 573, author="denis", title="rules.md exception limit", reviewers=(BOB,))
    h.tick()
    h.github.submit_review(API, 573, BOB)
    run(h, 1)
    h.github.add_reviewer(API, 573, BOB)
    run(h, 1)
    h.github.open(API, 566, author="nik", title="Skylos quality job", reviewers=(CAROL,))
    h.github.open(API, 586, author="axyi", title="No-text run as partial", reviewers=(BOB,))
    h.github.update(API, 586, labels=frozenset({"urgent"}))
    run(h, 1)
    (text,) = h.telegram.texts
    link = '<a href="https://github.com/acme/{}/pull/{}">{}</a>'
    assert text == "\n".join(
        [
            "🔎 Ревью ждут 5 PR",
            "",
            "@alice_tg",
            f"• {link.format('api', 562, 'api#562 Demo script v1.0')} · axyi",
            f"• {link.format('ui', 322, 'ui#322 RAG &lt;excerpts&gt;')} · axyi",
            "",
            "@bob_tg",
            f"• {link.format('api', 573, 'api#573 rules.md exception limit')} · denis · раунд 2",
            f"• 🔥 {link.format('api', 586, 'api#586 No-text run as partial')} · axyi · urgent",
            "",
            "@carol_tg",
            f"• {link.format('api', 566, 'api#566 Skylos quality job')} · nik",
            "",
            "/mine: твои PR сейчас",
        ]
    )


def test_only_people_with_new_prs_are_mentioned(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(ALICE,), labels=frozenset({"urgent"}))
    h.tick()
    h.github.open(API, 2, author=OUTSIDER, reviewers=(BOB,), labels=frozenset({"urgent"}))
    run(h, 1)
    assert len(h.telegram.texts) == 2
    assert "@alice_tg" not in h.telegram.texts[1]
    assert "@bob_tg" in h.telegram.texts[1] and "api#1 " not in h.telegram.texts[1]


# AC9: evening digest


def at_berlin(h: Harness, hour: int, minute: int = 0) -> None:
    # 16 Nov 2026: Berlin is UTC+1.
    h.clock.now = datetime(2026, 11, 16, hour - 1, minute, tzinfo=UTC)


def test_the_digest_at_18_lists_every_waiting_pr_with_age_only_past_10_hours(
    h: Harness,
) -> None:
    at_berlin(h, 6)
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,), labels=frozenset({"urgent"}))
    h.tick()  # notified at 06:00
    at_berlin(h, 16, 30)
    h.github.open(API, 2, author=OUTSIDER, reviewers=(CAROL,))
    h.tick()  # queued at 16:30, not sent yet
    at_berlin(h, 17, 59)
    h.tick()
    assert len(h.telegram.texts) == 1
    at_berlin(h, 18)
    h.tick()
    assert len(h.telegram.texts) == 2
    digest = h.telegram.texts[1]
    assert digest.startswith("🌙 Висит без ревью к 18:00\n\n")
    assert "api#1 Change 1</a> · dave-example · urgent · ждёт 12 ч" in digest
    assert "api#2 Change 2</a> · dave-example\n" in digest
    # Once a day, and the digest counts as the notice of the queued PR.
    for minute in range(1, 59):
        at_berlin(h, 18, minute)
        h.tick()
    at_berlin(h, 20)
    h.tick()
    assert len(h.telegram.texts) == 2


def test_no_digest_when_nothing_waits(h: Harness) -> None:
    at_berlin(h, 17, 50)
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.tick()
    h.github.submit_review(API, 1, BOB)
    at_berlin(h, 18)
    h.tick()
    at_berlin(h, 18, 30)
    h.tick()
    assert h.telegram.texts == []


def test_the_digest_is_not_repeated_after_a_restart(h: Harness) -> None:
    at_berlin(h, 10)
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.tick()
    at_berlin(h, 18)
    h.tick()
    h.restart()
    at_berlin(h, 18, 5)
    h.tick()
    assert len(h.telegram.texts) == 1


def test_starting_after_digest_window_does_not_send_digest(h: Harness) -> None:
    at_berlin(h, 19, 30)
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.tick()
    assert h.telegram.texts == []


def test_digest_is_sent_from_snapshot_when_github_poll_fails(h: Harness) -> None:
    at_berlin(h, 17, 50)
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.tick()
    assert h.telegram.texts == []
    h.github.fail_list = True
    at_berlin(h, 18, 0)
    h.tick()
    assert len(h.telegram.texts) == 1
    assert h.telegram.texts[0].startswith("🌙 Висит без ревью к 18:00\n\n")
    assert "api#1 Change 1" in h.telegram.texts[0]


# AC10: /mine


def command(update_id: int, text: str, username: str | None, chat: str = CHAT) -> Update:
    return Update(
        update_id=update_id, chat_id=chat, message_id=100 + update_id, username=username, text=text
    )


def test_mine_lists_the_senders_prs_with_links(h: Harness) -> None:
    h.github.open(API, 1, author=OUTSIDER, reviewers=(BOB,))
    h.github.open(UI, 2, author=OUTSIDER, reviewers=(BOB, ALICE))
    h.github.open(UI, 3, author=OUTSIDER, reviewers=(ALICE,))
    h.tick()
    h.bot.handle_updates([command(7, "/mine@review_bot", "Bob_TG")])
    ((chat, text, reply_to),) = h.telegram.sent
    assert chat == CHAT and reply_to == 107
    assert text.startswith("Твои PR сейчас: 2\n\n")
    assert '<a href="https://github.com/acme/api/pull/1">api#1 Change 1</a>' in text
    assert '<a href="https://github.com/acme/ui/pull/2">ui#2 Change 2</a>' in text
    assert "ui#3" not in text


def test_mine_for_nothing_waiting_and_for_an_unknown_sender(h: Harness) -> None:
    h.bot.handle_updates([command(1, "/mine", "carol_tg"), command(2, "/mine", "stranger")])
    assert h.telegram.texts == [
        "На тебе сейчас нет PR, которые ждут ревью.",
        "Не нашёл тебя в составе команды бота: попроси добавить твой Telegram-ник.",
    ]


def test_commands_from_other_chats_and_old_updates_are_ignored(h: Harness) -> None:
    h.telegram.updates = [command(1, "/mine", "bob_tg", chat="42"), command(2, "hi", "bob_tg")]
    assert h.bot.poll_commands(timeout=0)
    h.telegram.updates.append(command(3, "/mine", "bob_tg"))
    h.restart()
    assert h.bot.poll_commands(timeout=0)
    assert h.bot.poll_commands(timeout=0)
    assert len(h.telegram.texts) == 1
