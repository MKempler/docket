"""Platform adapters for owner cards: Telegram, Discord, Slack.

Each adapter receives the platform's native client from Hermes (ctx.register_platform_handler), draws a
Card with that platform's buttons, and relays taps to owner.perform via `tap`. Platform SDKs are imported
inside the methods, so Docket loads even when a platform library is not installed.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any, Callable

from .owner import ActionResult, Card, Channel

logger = logging.getLogger(__name__)

Tap = Callable[[str, str, str], ActionResult]     # (data, platform, user_id) -> result


class TelegramChannel(Channel):
    platform = "telegram"

    def __init__(self, tap: Tap):
        super().__init__()
        self.tap, self.app = tap, None

    def factory(self, app: Any, adapter: Any = None) -> None:
        from telegram.ext import CallbackQueryHandler
        self.app = app
        self.loop = asyncio.get_running_loop()
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^docket:"))

    def markup(self, card: Card):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        if not card.buttons:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton(b.label, callback_data=b.data) for b in row]
                                     for row in card.buttons])

    async def _send(self, owner: dict, card: Card) -> None:
        kwargs = {"message_thread_id": int(owner["thread_id"])} if owner.get("thread_id") else {}
        chat = int(owner["chat_id"]) if owner["chat_id"].lstrip("-").isdigit() else owner["chat_id"]
        if card.png:
            await self.app.bot.send_photo(chat_id=chat, photo=io.BytesIO(card.png), caption=card.caption[:1024],
                                          reply_markup=self.markup(card), **kwargs)
        else:
            await self.app.bot.send_message(chat_id=chat, text=card.text[:4096], reply_markup=self.markup(card), **kwargs)

    async def on_callback(self, update: Any, context: Any) -> None:
        query = update.callback_query
        result = await asyncio.get_running_loop().run_in_executor(
            None, self.tap, query.data, self.platform, str(query.from_user.id))
        await query.answer(result.toast[:190], show_alert=not result.ok)
        if result.card is not None:
            text = result.card.caption if query.message and query.message.photo else result.card.text
            try:
                if query.message and query.message.photo and result.card.png:
                    from telegram import InputMediaPhoto      # redraw the image so its status badge updates
                    await query.edit_message_media(
                        media=InputMediaPhoto(media=io.BytesIO(result.card.png), caption=text[:1024]),
                        reply_markup=self.markup(result.card))
                elif query.message and query.message.photo:
                    await query.edit_message_caption(caption=text[:1024], reply_markup=self.markup(result.card))
                else:
                    await query.edit_message_text(text=text[:4096], reply_markup=self.markup(result.card))
            except Exception:
                logger.debug("Docket: card edit skipped", exc_info=True)
        if result.followup and query.message:
            await query.message.reply_text(result.followup)
        if result.forward and query.message:           # its own message: long-press -> Forward / Copy
            await query.message.reply_text(result.forward, disable_web_page_preview=True)


class DiscordChannel(Channel):
    platform = "discord"

    def __init__(self, tap: Tap):
        super().__init__()
        self.tap, self.bot = tap, None

    def factory(self, bot: Any, adapter: Any = None) -> None:
        self.bot = bot
        self.loop = asyncio.get_running_loop()
        bot.add_listener(self.on_interaction, "on_interaction")

    def view(self, card: Card):
        import discord
        if not card.buttons:
            return None
        styles = {"primary": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}
        view = discord.ui.View(timeout=None)
        for row_index, row in enumerate(card.buttons):
            for b in row:
                view.add_item(discord.ui.Button(label=b.label, custom_id=b.data, row=row_index,
                                                style=styles.get(b.style, discord.ButtonStyle.secondary)))
        return view

    async def _send(self, owner: dict, card: Card) -> None:
        import discord
        target = self.bot.get_channel(int(owner["chat_id"])) or await self.bot.fetch_channel(int(owner["chat_id"]))
        kwargs: dict[str, Any] = {"view": self.view(card)} if card.buttons else {}
        if card.png:
            kwargs["file"] = discord.File(io.BytesIO(card.png), filename="quote.png")
            await target.send(content=card.caption[:2000], **kwargs)
        else:
            await target.send(content=card.text[:2000], **kwargs)

    async def on_interaction(self, interaction: Any) -> None:
        data = getattr(interaction, "data", None) or {}
        custom_id = str(data.get("custom_id") or "")
        if not custom_id.startswith("docket:"):
            return
        await interaction.response.defer()
        result = await asyncio.get_running_loop().run_in_executor(
            None, self.tap, custom_id, self.platform, str(interaction.user.id))
        if result.card is not None and interaction.message is not None:
            try:
                has_image = bool(interaction.message.attachments)
                kwargs: dict[str, Any] = {"view": self.view(result.card)}
                if has_image and result.card.png:           # redraw the image so its status badge updates
                    import discord
                    kwargs["attachments"] = [discord.File(io.BytesIO(result.card.png), filename="quote.png")]
                await interaction.message.edit(content=(result.card.caption if has_image else result.card.text)[:2000],
                                               **kwargs)
            except Exception:
                logger.debug("Docket: card edit skipped", exc_info=True)
        message = result.followup or ("" if result.ok else result.toast)
        if message:
            await interaction.followup.send(message, ephemeral=not result.ok)
        if result.forward:
            await interaction.followup.send(result.forward)


class SlackChannel(Channel):
    platform = "slack"

    def __init__(self, tap: Tap):
        super().__init__()
        self.tap, self.app = tap, None

    def factory(self, app: Any, adapter: Any = None) -> None:
        self.app = app
        self.loop = asyncio.get_running_loop()

    @staticmethod
    def blocks(card: Card) -> list[dict]:
        blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": card.text[:2900]}}]
        for row in card.buttons:
            elements = []
            for b in row:
                el = {"type": "button", "text": {"type": "plain_text", "text": b.label[:75], "emoji": True},
                      "action_id": b.data, "value": b.data}
                if b.style in {"primary", "danger"}:
                    el["style"] = b.style
                elements.append(el)
            blocks.append({"type": "actions", "elements": elements})
        return blocks

    async def _send(self, owner: dict, card: Card) -> None:
        kwargs = {"thread_ts": owner["thread_id"]} if owner.get("thread_id") else {}
        await self.app.client.chat_postMessage(channel=owner["chat_id"], text=card.caption, blocks=self.blocks(card),
                                               **kwargs)

    async def on_action(self, ack: Any, body: dict, action: dict) -> None:
        await ack()
        data = str(action.get("value") or action.get("action_id") or "")
        result = await asyncio.get_running_loop().run_in_executor(
            None, self.tap, data, self.platform, str((body.get("user") or {}).get("id", "")))
        channel = (body.get("channel") or {}).get("id")
        ts = (body.get("message") or {}).get("ts")
        if self.app is None or not channel:
            return
        if result.card is not None and ts:
            await self.app.client.chat_update(channel=channel, ts=ts, text=result.card.caption,
                                              blocks=self.blocks(result.card))
        message = result.followup or ("" if result.ok else result.toast)
        if message:
            await self.app.client.chat_postMessage(channel=channel, thread_ts=ts, text=message)
        if result.forward:
            await self.app.client.chat_postMessage(channel=channel, thread_ts=ts, text=result.forward,
                                                   unfurl_links=False)
