"""Tests of what a context card says about its thread (pensieve/cardtext.py). Run: python tests/card_text.py

Each test is one case found in real cards. Its docstring gives the rule that it checks. The cards and transcripts
are made up."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve.cardtext import Anchor, Pending, Reply, agent_reply, anchor, branch_theme, my_messages, pending, \
    card_title, pick_session, turns_from_chunks  # noqa: E402

PR = "https://github.com/example/app/pull/"


def card(**kw):
    base = dict(id="t:X", title="", tickets=[], prs=[], branches=[], places=[], sessions=[], keys=[])
    return {**base, **kw}


def test_ticket_names_the_thread():
    """A card with a ticket is named by the ticket it solves, not by a session's newest message."""
    c = card(tickets=[dict(key="ABC-12", title="Stop retrying hard card declines", status="In Progress", updated=5)],
             sessions=[dict(id="s1", title="how do we join people?", updated=9)])
    assert anchor(c) == Anchor("ticket", "Stop retrying hard card declines", "ABC-12")


def test_the_branchs_ticket_wins():
    """With several tickets, the one that the branch names is the thread's ticket, even when another is newer."""
    c = card(tickets=[dict(key="ABC-7", title="Old cleanup", status="To Do", updated=99),
                      dict(key="ABC-12", title="Card retries", status="In Progress", updated=1)],
             branches=[dict(repo="/r", branch="abc-12-card-retries")])
    assert anchor(c).ref == "ABC-12"


def test_a_mentioned_ticket_is_not_the_anchor():
    """A ticket that the sessions only mention (ref) does not name the card."""
    c = card(tickets=[dict(key="ABC-9", title="", ref=True)], branches=[dict(repo="/r", branch="fix/login-timeout")])
    assert anchor(c) == Anchor("branch", "Login timeout", "fix/login-timeout")


def test_pr_names_the_thread_first():
    """The thread's own PR names it, before its ticket and its branch: no 'fix(scope):' prefix or key suffix, a capital
    first letter."""
    c = card(branches=[dict(repo="/r", branch="ops-1-retry-cards")],
             tickets=[dict(key="OPS-1", title="Payments retry forever", status="In Progress")],
             prs=[dict(url=PR + "4", title="fix(payments): stop on hard declines (OPS-1)", state="open", head="ops-1-retry-cards")])
    assert anchor(c) == Anchor("pr", "Stop on hard declines", PR + "4")


def test_first_pr_keeps_the_title():
    """The first PR of the thread (the lowest number) keeps naming it when a later PR opens or it merges: a title
    that follows PR states would change while the work goes on."""
    c = card(branches=[dict(repo="/r", branch="retry")],
             prs=[dict(url=PR + "9", title="Follow-up: log retries", state="open", head="retry"),
                  dict(url=PR + "4", title="Stop retrying hard declines", state="merged", head="retry")])
    assert anchor(c).title == "Stop retrying hard declines"


def test_ticket_choice_does_not_follow_updates():
    """With tickets that neither the branch nor a PR names, the lowest key names the thread, not the one edited last."""
    c = card(tickets=[dict(key="ABC-30", title="Later ticket", status="To Do", updated=99),
                      dict(key="ABC-12", title="First ticket", status="To Do", updated=1)])
    assert anchor(c).ref == "ABC-12"


def test_another_cards_pr_does_not_name_it():
    """A PR from another branch that the sessions talk about is not the card's own: the branch names it."""
    c = card(branches=[dict(repo="/r", branch="sam/retry-cards")],
             prs=[dict(url=PR + "9", title="Unrelated", state="merged", head="other-branch")])
    assert anchor(c).kind == "branch" and anchor(c).title == "Retry cards"


def test_branch_theme_words():
    """A branch name becomes words: type and user prefixes go, dashes and underscores become spaces."""
    assert branch_theme("feature/sam/retry-card_payments") == "Retry card payments"
    assert branch_theme("fix/login-timeout") == "Login timeout"
    assert branch_theme("main") == "Main"


def test_goal_card_keeps_its_goal():
    """A goal split from another card shares that card's sessions, so it keeps its own goal title (case M)."""
    c = card(id="b:/r@x~pr4", goal_of="b:/r@x", title="Ship the export button",
             tickets=[dict(key="ABC-1", title="Parent ticket", status="In Progress")])
    assert anchor(c) == Anchor("goal", "Ship the export button", "b:/r@x~pr4")


def test_session_only_card_starts_from_its_session():
    """With no ticket, PR or branch, the session's own title is a starting point that the note can improve."""
    c = card(sessions=[dict(id="s1", title="Explore the caching options", updated=3)])
    assert anchor(c) == Anchor("session", "Explore the caching options", "s1", theme=True)


def test_newest_own_session():
    """The card's own session with the newest activity is used (6 cards have 2 or more sessions)."""
    c = card(sessions=[dict(id="old", updated=10), dict(id="new", updated=50), dict(id="mid", updated=30)])
    assert pick_session(c)["id"] == "new"


def test_shared_sessions_do_not_count():
    """A session that is only shared with the card (it worked on the branch in passing) is not used (case B)."""
    c = card(sessions=[dict(id="own", updated=10)], shared_sessions=[dict(id="long-session", at=99)])
    assert pick_session(c)["id"] == "own"


def test_no_session():
    """A card made of a ticket or a PR only has no session (49 cards, case A)."""
    assert pick_session(card()) is None


def test_commands_and_system_text_are_skipped():
    """Slash commands, their output, hook notes and interruptions are not the user's words (11 cards, case E)."""
    turns = [("user", "Make the retry stop on hard declines"), ("assistant", "Done, see the diff above."),
             ("user", "<command-name>/clear</command-name>"), ("user", "[Request interrupted by user]"),
             ("user", "<system-reminder>a hook said this</system-reminder>")]
    assert my_messages(turns) == ["Make the retry stop on hard declines"]


def test_paste_is_dropped_but_my_words_stay():
    """A pasted block goes, and the user's words around it stay (6 cards, case F)."""
    turns = [("user", "why does this fail? <pasted_content id=\"1\">Traceback ... 400 lines</pasted_content id=\"1\">")]
    assert my_messages(turns) == ["why does this fail?"]


def test_trivial_replies_are_skipped():
    """'ok', 'continue' and 'commit and push' carry no topic: the last substantive messages are used (cases G, H)."""
    turns = [("user", "Add a retry limit of three"), ("assistant", "Added it."), ("user", "Also log each retry"),
             ("assistant", "Logged."), ("user", "ok"), ("user", "commit and push")]
    assert my_messages(turns) == ["Add a retry limit of three", "Also log each retry"]


def test_long_message_keeps_start_and_end():
    """A very long message keeps its start and its end (7 cards, case I)."""
    text = "START " + "x" * 3000 + " END"
    [m] = my_messages([("user", text)], n=1)
    assert m.startswith("START") and m.endswith("END") and len(m) < 700


def test_only_noise():
    """A session with only commands and 'ok' has no message to use."""
    assert my_messages([("user", "<command-name>/clear</command-name>"), ("user", "ok")]) == []


def test_last_reply_after_my_last_message():
    """The agent's last substantive reply after the user's last substantive message says where it stopped."""
    turns = [("user", "Make it retry"), ("assistant", "Retries now stop on hard declines and log each attempt."),
             ("user", "thanks")]
    assert agent_reply(turns) == Reply("Retries now stop on hard declines and log each attempt.", "")


def test_reply_question():
    """A reply that ends with a question gives the question (14 cards, case L)."""
    turns = [("user", "Fix the flaky test"),
             ("assistant", "The test waits on a real clock.\n\nShould I mock the clock or raise the timeout?")]
    assert agent_reply(turns).question == "Should I mock the clock or raise the timeout?"


def test_no_reply_yet():
    """No reply after the user's last message: the agent is working, or was stopped. The reply before is kept as
    context (11 cards, case J)."""
    turns = [("user", "Look at the logs"), ("assistant", "The logs show a timeout in the payment client."),
             ("user", "Now fix the timeout")]
    r = agent_reply(turns)
    assert r.waiting and r.text == "The logs show a timeout in the payment client."


def test_short_reply_is_skipped():
    """'Done.' says little: the last reply of some length is used (2 cards, case K)."""
    turns = [("user", "Ship it please"), ("assistant", "Merged #4 and closed ABC-12 after the checks passed."),
             ("assistant", "Done.")]
    assert agent_reply(turns).text.startswith("Merged #4")


def test_pending_pr_states():
    """Each open PR gives the step that moves it forward, and who can take it."""
    c = card(prs=[dict(url=PR + "1", state="draft"), dict(url=PR + "2", state="open", review="approved"),
                  dict(url=PR + "3", state="open", checks="failing"), dict(url=PR + "4", state="open"),
                  dict(url=PR + "5", state="open", review="changes requested"), dict(url=PR + "6", state="merged")])
    got = [(p.text, p.how) for p in pending(c)]
    assert ("Mark #1 ready for review", "you") in got
    assert ("Merge #2", "you") in got
    assert ("Fix the failing checks on #3", "agent") in got and ("Get #3 reviewed", "you") in got
    assert ("Get #4 reviewed", "you") in got
    assert ("Address the review on #5", "agent") in got and not any("#5 reviewed" in t for t, _ in got)
    assert not any("#6" in t for t, _ in got)


def test_merged_pr_means_close_the_ticket():
    """A ticket that is not done although its PR merged: the next step is to close it in its tracker. Pensieve only
    reads, so this is a hint for the user or their agent ('parent'), not a change Pensieve makes."""
    c = card(tickets=[dict(key="ABC-12", title="Retries", status="Needs Triage")],
             prs=[dict(url=PR + "4", state="merged")])
    assert pending(c) == [Pending("Close ABC-12: its pull request merged", "parent", "ABC-12")]


def test_open_ticket_with_no_pr_is_the_work():
    """No PR and an open ticket: the ticket is still the work to do."""
    c = card(tickets=[dict(key="OPS-3", title="Nightly job fails", status="In Progress")])
    assert pending(c) == [Pending("Work on OPS-3", "agent", "OPS-3")]


def test_done_ticket_has_nothing_left():
    """A ticket in a done state leaves nothing to do."""
    c = card(tickets=[dict(key="ABC-12", title="Retries", status="Done")], prs=[dict(url=PR + "4", state="merged")])
    assert pending(c) == []


def test_unpushed_and_uncommitted_on_own_branch():
    """Commits and changes on the card's own branch are left to push and commit; another branch open in the same
    folder (head) is not the card's work."""
    c = card(branches=[dict(repo="/r", branch="retry")],
             places=[dict(branch="retry", changed=2, ahead=1), dict(branch="retry", head="main", changed=9)])
    got = [p.text for p in pending(c)]
    assert got == ["Commit 2 changed files on retry", "Push 1 commits on retry", "Open a pull request for retry"]


def test_discard_steps():
    """Discarding closes without a merge: drop the changes, close the open PR, delete the branch (and its remote copy
    when pushed), cancel the ticket that is not done."""
    from pensieve.cardtext import discard
    c = card(branches=[dict(repo="/r", branch="retry")], places=[dict(branch="retry", changed=2, upstream=True)],
             prs=[dict(url=PR + "4", state="open", head="retry"), dict(url=PR + "3", state="merged", head="old")],
             tickets=[dict(key="ABC-1", status="In Progress"), dict(key="ABC-2", status="Done")])
    assert [s["text"] for s in discard(c)] == ["Discard 2 uncommitted files on retry", "Close #4 without merging",
                                               "Delete branch retry (local and remote)", "Mark ABC-1 as cancelled (won't do)"]
    assert [s["who"] for s in discard(c)] == ["agent", "you", "agent", "parent"]
    assert discard(card()) == []


def test_branch_with_a_pull_request_needs_none():
    """A branch that has a pull request, open or merged, needs no new one; a branch with nothing on it needs none
    either."""
    c = card(branches=[dict(repo="/r", branch="retry")], places=[dict(branch="retry", commits=3)],
             prs=[dict(url=PR + "4", state="open", head="retry")])
    assert [p.text for p in pending(c)] == ["Get #4 reviewed"]
    bare = card(branches=[dict(repo="/r", branch="retry")], places=[dict(branch="retry")])
    assert pending(bare) == []


def test_agent_question_comes_first():
    """An unanswered question of the agent blocks the thread, so it comes first, and only the user can answer."""
    c = card(prs=[dict(url=PR + "4", state="open")])
    got = pending(c, Reply("…", "Mock the clock or raise the timeout?"))
    assert got[0] == Pending("Answer the agent: Mock the clock or raise the timeout?", "you")


def test_ticket_title_without_queue_tags():
    """'[on-call] [Data] ' at the start of a ticket title names a queue, not the thread: it goes."""
    c = card(tickets=[dict(key="OPS-9", title="[on-call] [Data] Nightly export fails", status="To Do")])
    assert anchor(c).title == "Nightly export fails"


def test_one_ticket_from_two_sources():
    """The same ticket from its tracker and from a session that read it is one item left, not two."""
    c = card(tickets=[dict(key="OPS-3", title="Job fails", status="Needs Triage"),
                      dict(key="OPS-3", title="Job fails", status="Needs Triage")])
    assert pending(c) == [Pending("Work on OPS-3", "agent", "OPS-3")]


def test_card_title():
    """A thread shows its anchor's title, which stays the same while the thread goes on. A thread with no anchor
    title shows its own."""
    t = dict(anchor=dict(kind="ticket", title="Stop retrying hard declines"), title="t")
    assert card_title(t) == "Stop retrying hard declines"
    b = dict(anchor=dict(kind="branch", title="Retry cards"), title="b")
    assert card_title(b) == "Retry cards"
    assert card_title(dict(anchor={}, title="own words")) == "own words"
    s = dict(anchor=dict(kind="session", title=""), title="Explore caching")
    assert card_title(s) == "Explore caching"


def test_turns_from_chunks():
    """A session's stored chunks give its turns back: each "User:"/"Assistant:" starts a turn, and a chunk that starts
    inside a long turn (cut on a paragraph) continues it."""
    chunks = ["User: Fix the timeout\n\nAssistant: Found it in the client.", "It retries without a limit.",
              "User: ok\n\nAssistant: Added a limit of three."]
    assert turns_from_chunks(chunks) == [("user", "Fix the timeout"),
                                         ("assistant", "Found it in the client.\n\nIt retries without a limit."),
                                         ("user", "ok"), ("assistant", "Added a limit of three.")]



if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
