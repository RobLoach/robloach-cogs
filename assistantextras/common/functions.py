import asyncio
import json
from datetime import datetime
from io import StringIO
from typing import Literal

import discord
from redbot.core import commands

from ..abc import MixinMeta

class Functions(MixinMeta):
    async def wordle(
        self,
        guild: discord.Guild,
        user: discord.Member,
        *args,
        **kwargs,
    ):
        return "Starting worldle!"
