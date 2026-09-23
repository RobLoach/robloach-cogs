"""
Every line this cog writes on a game's message, and the sanitising a name
goes through before it can be one.

Pulled out of retro/RetroView.py, which had grown to 2,700 lines with a third
of them not about the view at all. What is here is the *words*: the one line
above the clip is assembled from these, and the two things that go into it
that somebody else chose -- a display name and a ROM's filename -- are made
safe here too.

Two things are deliberately not here. The queue's, undo's and the repeat
button's *policy* constants stay with the mechanisms they bound, in
RetroView.py and timing.py; only the sentences moved. And a handful of one-off
sentences are still written inline in the branch that says them (a refused
press, an empty undo, a replaced controller) -- they are each said in exactly
one place, and a name in this file would only be read by the line that
follows it.

RetroView re-exports every name in here, so `retro.RetroView.presser_name`
still resolves; see the note at the top of that file.
"""

import re
import typing
import unicodedata

import discord

#: Said once, on the next line the session writes, when the repeat button has
#: had to go. A control that vanishes with no explanation reads as removed
#: just as surely as a greyed-out one does -- that is precisely how the
#: greyed-out version was reported -- so the moment it goes is the moment to
#: say why, and where it went. It rides on an edit that was happening anyway.
REPEAT_GONE_NOTE = (
    "The **{button} x3** button is hidden while clips are this short: only "
    "one tap fits, which is what **{button}** already does. A longer "
    "`cliplength` brings it back."
)

#: Said privately to whoever clicks a repeat button that is not drawn any
#: more -- i.e. one still sitting on a message Discord has not re-rendered.
#:
#: The click arrives because the button never left the view (see the note
#: above _STYLES); this is what makes arriving worth something. Answering it
#: with a silent defer would be indistinguishable from the controller
#: ignoring the click, which is the complaint the press queue exists to fix,
#: and pressing the confirm button on their behalf would be doing something
#: they did not ask for. So it says what happened and what to press instead,
#: to them and to nobody else: one interaction response, no edit of the
#: message, and no rewind of the clip that is playing on it.
#:
#: It says no number, deliberately. The button on the message they clicked
#: says "x2" or "x3" depending on how long the clip was when that message
#: was last drawn, and the view no longer knows which -- an interaction
#: carries the custom_id and not the label. Naming the count would therefore
#: be a guess, and one that contradicts what they are looking at.
REPEAT_STALE_NOTE = (
    "The repeat button is hidden while clips are this short, and this "
    "message has not been redrawn yet \N{EM DASH} only one tap fits at this "
    "`cliplength`, which is what **{button}** already does. Press "
    "**{button}**, or ask for a longer `cliplength` to bring the repeat "
    "button back."
)


# What a press says when it had to wake the session up first. Past tense on
# purpose: it is shown *with* the clip rather than before it, because a press
# makes exactly one edit to the message now (see RetroView._ack_now), so by
# the time anybody reads it the game is already back. Cleared by the next
# press, like every other one-off line.
#
# It is the *only* thing that says a session was asleep, and it arrives at
# the one moment that is worth: a wake is the longest wait in the cog (a core
# to dlopen, a save state to load), and this line is written at the end of it
# on the edit that press was making anyway.
#
# There used to be a second half -- an ASLEEP_MARK appended to the header for
# the whole time a session had no core -- and it is gone. It was accurate and
# unhelpful. Sleeping is an implementation detail of fitting one emulator
# across every channel: the controls stay live, and the next press wakes the
# game with no more ceremony than any other press. Labelling a working
# controller "asleep" invites somebody to think it is broken or that they
# have to do something to it first, which is the opposite of true.
RESUMED_NOTE = "Woke up where you left off."

#: How the game is named on the line above the clip. A stable prefix rather
#: than a status card: the first clip of a cold boot used to go out with *no*
#: text at all, and after that the only text was the press line, so somebody
#: scrolling past saw an animation, a grid of buttons and "Rob pressed A."
#: with nothing anywhere saying what game it was.
#:
#: It rides on the content that is already rewritten by every press, so it
#: costs nothing: no card, no embed, no extra edit. One line, always.
#:
#: **The console used to be in here** and is not any more. The line read
#: ``**µCity** · Game Boy — Rob pressed A.`` and now reads
#: ``**µCity** · Rob pressed A.``: the game's name is the thing nobody could
#: work out from the picture, and the console is the thing everybody can --
#: it is on screen in the boot logo, in the shape of the frame, and in the
#: controller laid out underneath. It was costing a third of a short line to
#: repeat it on every press. `[p]retro` on its own still lists every console
#: this bot emulates, which is where somebody actually asks.
HEADER = "**{game}**"

#: What separates the header from whatever just happened. The same middle dot
#: that separates the pieces of the header, so the whole line is one list of
#: short facts rather than a header and a sentence joined by a dash.
HEADER_SEPARATOR = " \N{MIDDLE DOT} "

# How much of a game's name goes in the header. Game names come from a ROM
# filename that has already been through Retro._sanitize_filename (which caps
# it at 64 characters), so this is a second, independent bound for a name that
# arrives from a stored session record written by an older version.
MAX_GAME_NAME = 48

# -- The queue's own words ----------------------------------------------------
#
# What is waiting, and what a discard threw away. The queue itself -- how deep
# it goes, who may have an entry in it, and what empties it -- belongs to the
# session; see the note above MAX_QUEUED_PRESSES in RetroView.py. These are
# the two suffixes it writes, and both of them ride on a line somebody else's
# press is rewriting anyway, which is why a queued press needs no message of
# its own.

#: What a queued Wait is called in the listing. The other entries name the
#: console's own button (or the d-pad's arrow) through System.caption_for,
#: exactly as the press line does.
QUEUED_WAIT = "wait"

#: The suffix that shows what is waiting. Italic and parenthetical on purpose:
#: the sentence in front of it is what just happened, and this is a footnote
#: to it rather than a second announcement.
QUEUE_NOTE = "*Queued: {queued}*"

#: One person's run of waiting presses: their name, then their buttons in
#: order, separated by nothing at all -- ``Rob ⬆️⬆️⬇️⬇️``.
#:
#: The name is back, and a run is grouped rather than listed one entry at a
#: time, because one person may now hold every slot (see MAX_QUEUED_PRESSES).
#: Four separate entries reading ``⬆️, ⬆️, ⬇️, ⬇️`` says four unrelated
#: things happened; ``Rob ⬆️⬆️⬇️⬇️`` says what it actually is, which is one
#: person walking. Grouping also keeps the line short at the length that
#: matters: the queue is at most MAX_QUEUED_PRESSES deep, so the worst case
#: is one name and three buttons.
#:
#: Consecutive entries by the same person are grouped; a different person
#: starts a new group, so ``Rob ⬆️⬆️, Ada ⬅️`` is two people and the order
#: is still the order they will run in. A name that :func:`presser_name`
#: could not read at all is left out and only the buttons are shown.
QUEUE_ENTRY = "{who} {buttons}"
QUEUE_ENTRY_ANONYMOUS = "{buttons}"

#: Said once, on the next line the session writes, when something threw the
#: queue away: a reset, an undo, a stop. Without it the presses simply
#: vanish, which is the bug this whole mechanism exists to fix -- so the one
#: case where dropping them is *right* has to say so out loud.
DROPPED_NOTE = "*{count} queued press{plural} dropped*"

# -- Saying who did what ------------------------------------------------------
#
# Every action names itself, and its author, on the message it edits: one
# short line above the clip, in one voice for all five of them --
#
#     Rob pressed A.
#     Rob pressed \N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}.
#     Rob pressed A x3.
#     Rob waited.
#     Rob undid the last press.
#     Rob reset the game.
#
# -- and the impersonal form of the same sentence ("Pressed A.", "Waited.")
# whenever there is no name to use, which is the one thing that must never
# fail: a click always gets a line, even from a user object that turns out to
# have no usable name at all.
#
# It rides on the one edit the action already makes (see RetroView._show), so
# it costs nothing: no extra request, no extra rate-limit budget, and nothing
# that could re-render the message a second time and rewind the clip.
#
# The name of a *button* comes from the console's own Button in systems.py,
# via System.caption_for -- never from a second table here. That is what makes
# a Genesis press read "pressed C." rather than "pressed A." (its C is
# RetroPad *a*) and what keeps the line in step with the label on the button
# that was clicked. The d-pad has no labels at all, so it names itself with
# its arrow emoji, which has already been through systems.validate_emoji() at
# import time; a bare codepoint with no U+FE0F is what caused a 400 in
# production once, and it cannot get in here without failing that check first.
#
#: action -> (the line when the author is known, the line when it is not).
#: Table-driven on purpose: five actions in one voice is a property of this
#: dict rather than of five string literals scattered through the file, and
#: the tests read it back.
ACTION_NOTES: typing.Dict[str, typing.Tuple[str, str]] = {
    "press": ("{who} pressed {button}.", "Pressed {button}."),
    "wait": ("{who} waited.", "Waited."),
    "undo": ("{who} undid the last press.", "Undid the last press."),
    "reset": ("{who} reset the game.", "Reset the game."),
}

# How much of a display name goes on the line. 32 is Discord's own ceiling for
# both a nickname and a global display name, so no real name is ever cut; the
# cap is here for a name that arrives from somewhere else (a cached member
# object, a future API, a test) and to make "one presser cannot own the line"
# a property of this module rather than a hope about Discord's limits. The
# worst case is therefore a 32 character name plus " undid the last press.",
# which is 54 characters -- still one short line above the clip.
MAX_PRESSER_NAME = 32

# Unicode general categories that take up no space on screen: control
# characters (Cc, which includes the newlines that would turn one line into
# three), format characters (Cf, which is the zero-width space, the
# zero-width joiner, the soft hyphen, the byte-order mark and the
# right-to-left override), lone surrogates, private use, unassigned, and the
# line/paragraph separators.
#
# Whitespace is handled before this, so a name written with newlines or tabs
# in it still reads as separate words rather than having them run together.
INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

# Every character Discord reads as markup, escaped unconditionally with a
# backslash -- which Discord renders as the plain character, so "Jean\-Luc"
# reads "Jean-Luc" and the only cost is a backslash nobody sees.
#
# Deliberately a table here rather than discord.utils.escape_markdown, for
# two reasons that both matter for a *name*:
#
# * that helper defaults to ignore_links=True and leaves markdown inside
#   anything URL-shaped alone, so a display name of
#   "http://example.com/__x__" would come back unescaped. A display name is
#   not prose with links in it;
# * it also leaves the start-of-line syntax alone (`#` headers, `-`/`+`
#   lists, `>` quotes), which is exactly where a presser's name sits. A
#   display name of "# hello" would render the whole line as a header --
#   precisely the "one user dominates the line" problem the length cap is
#   for.
#
# (On Python 3.13 it additionally emits a DeprecationWarning from inside
# discord.py, once per call, which a cog should not be minting on every
# button press.)
#
# `<` is in here for the family of things angle brackets open: `<@123>` (a
# mention), `<#123>` (a channel), `<:name:123>` (a custom emoji) and `<t:0>`
# (a timestamp). Brackets and parentheses are for `[text](url)` masked links.
# The backslash itself is first, and a single str.translate pass maps every
# character from the *input*, so escaping it cannot cascade into the
# backslashes this adds.
#
# @everyone, @here and <@id> are still handed to discord.py's own
# escape_mentions afterwards: it is the maintained answer for those, it is
# warning-free, and a second opinion on the one class of markup that can
# actually notify somebody is worth having.
MARKDOWN_ESCAPES = str.maketrans(
    {character: f"\\{character}" for character in "\\*_~`|#-+<>[]()"}
)

#: A leading "1." starts an ordered list, so the stop after a leading run of
#: digits is escaped too. The digits themselves are fine, and "1)" is already
#: covered by the bracket in MARKDOWN_ESCAPES.
LEADING_ORDINAL = re.compile(r"^(\d+)(\.)")

# Nothing this cog writes may ping anybody, and the press line is why: a
# notification on every button press, from everybody in the channel, would be
# intolerable in a way that no amount of "well, it is only one line" fixes.
#
# Two independent guards, because one of them is a promise about a string and
# the other is a promise to Discord:
#
# * presser_name() emits *no mention syntax at all*. The name is plain text
#   with every mention-shaped thing escaped, so there is nothing for Discord
#   to resolve into a ping in the first place;
# * every edit that can carry a name also carries this, so even a line that
#   somehow contained a live mention could not deliver one.
#
# Belt and braces on purpose: the first guard is the one that matters and the
# second is the one that cannot be got wrong by a future edit to the wording.
NO_PINGS = discord.AllowedMentions.none()


def presser_name(user: typing.Any) -> str:
    """
    A person's display name, safe to drop into the middle of a sentence.

    Returns ``""`` for anybody who cannot be named, which is the caller's
    signal to use the impersonal form of the line. Never raises: a click must
    never fail because of whatever somebody called themselves.

    ``display_name`` is asked for first and works for every kind of author
    this cog sees -- a :class:`discord.Member` (their per-guild nickname), a
    plain :class:`discord.User` with no guild at all (their global display
    name, or their username), and a member object left over from somebody who
    has since left the guild (the nickname Discord last told us about). The
    two fallbacks after it are for an object that has only one of the others.

    What comes back is then made safe, in this order:

    1. **whitespace is collapsed**, so a name with a newline in it cannot
       turn one line above the clip into three;
    2. **invisible characters are dropped** (see INVISIBLE_CATEGORIES): a
       name made entirely of zero-width spaces is indistinguishable from
       having no name, and is treated as such rather than producing
       " pressed A.";
    3. **the length is capped** at MAX_PRESSER_NAME, before escaping, so a
       trailing backslash can never be cut off from the character it escapes;
    4. **markdown and mentions are escaped** -- MARKDOWN_ESCAPES for every
       character Discord reads as markup, LEADING_ORDINAL for the one piece
       of it that is positional, and discord.py's ``escape_mentions`` for
       ``@everyone``/``@here``/``<@id>``. The result contains no mention
       syntax at all, which is the first of the two guarantees described
       above NO_PINGS.
    """
    raw = ""
    for attribute in ("display_name", "global_name", "name"):
        value = getattr(user, attribute, None)
        if isinstance(value, str) and value.strip():
            raw = value
            break
    if not raw:
        return ""

    characters = []
    for character in raw:
        if character.isspace():
            characters.append(" ")
        elif unicodedata.category(character) not in INVISIBLE_CATEGORIES:
            characters.append(character)
    name = " ".join("".join(characters).split())
    if not name:
        return ""
    if len(name) > MAX_PRESSER_NAME:
        name = name[: MAX_PRESSER_NAME - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"

    return escape_label(name)


def escape_label(name: str) -> str:
    """
    Make a name safe to drop into a sentence Discord will render.

    MARKDOWN_ESCAPES for every character Discord reads as markup,
    LEADING_ORDINAL for the one piece of it that is positional, and
    discord.py's ``escape_mentions`` for ``@everyone``/``@here``/``<@id>``.
    The result contains no mention syntax at all.

    Shared by :func:`presser_name` and :meth:`RetroView.header`, because the
    two things that go on that one line -- who clicked and what game it is --
    have exactly the same problem: a game called ``__x__`` or a nickname of
    ``# hello`` would otherwise reformat the line it sits on.
    """
    escaped = LEADING_ORDINAL.sub(r"\1\\\2", str(name).translate(MARKDOWN_ESCAPES))
    return discord.utils.escape_mentions(escaped)


def action_note(action: str, user: typing.Any = None, button: str = "") -> str:
    """
    The one line an action puts on the message, with its author's name in it.

    ``action`` is a key of ACTION_NOTES. The impersonal form is used whenever
    :func:`presser_name` cannot name the author, so this always returns a
    sentence.
    """
    named, plain = ACTION_NOTES[action]
    who = presser_name(user)
    return (named if who else plain).format(who=who, button=button)
