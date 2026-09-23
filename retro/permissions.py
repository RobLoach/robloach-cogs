"""
Who may do the things that are not open to everybody.

One function, and a module of its own for it, because it is the one place
this cog answers a *permission* question and it had been answered twice
before (see below). Pulled out of retro/RetroView.py, where it had nothing to
do with the view it was sitting in: the commands that ask it -- `[p]retrosleep`,
`[p]retroreboot`, `[p]retroend` and the `[p]retrosaves` group -- are spread
across Retro.py and saves.py, and only one of them goes anywhere near a
button.

No discord.py import: the two things it looks at (an id and a
``guild_permissions``) are duck-typed on purpose; see below.

RetroView re-exports it, so `retro.RetroView.may_manage` still resolves.
"""

import typing


async def may_manage(bot, user: typing.Any, starter_id: typing.Optional[int]) -> bool:
    """
    Whether this person may do the things that are not open to everybody.

    Anyone in the channel can play. Putting someone else's game to sleep
    (`[p]retrosleep`), rebooting it (`[p]retroreboot`), finishing with it
    (`[p]retroend`) and destroying or replacing its saves (the `[p]retrosaves`
    group) all cost the whole channel its progress-in-flight, so they are the
    same three people: whoever started the game, anybody who can moderate the
    channel, and the bot owner.

    **The one implementation.** It used to be two -- `RetroView.can_stop` and
    `SavesMixin._may_manage_saves` -- which each claimed to be the single
    source and checked the same three things in different orders.

    The cheap checks come first, so ``is_owner`` (which can hit Red's config)
    is only reached for somebody who is neither the starter nor a moderator.
    ``guild_permissions`` is duck-typed rather than gated on
    ``isinstance(user, discord.Member)``: a Member has the attribute and a
    plain User does not, which *is* the question being asked, and an
    isinstance check on a library class only makes the branch impossible to
    exercise in a test.
    """
    if user is None:
        return False
    if starter_id and getattr(user, "id", None) == starter_id:
        return True
    permissions = getattr(user, "guild_permissions", None)
    if permissions is not None and getattr(permissions, "manage_messages", False):
        return True
    return bool(await bot.is_owner(user))
