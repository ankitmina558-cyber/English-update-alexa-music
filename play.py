import random
import time

from pyrogram import filters, StopPropagation
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from pytgcalls.types.input_stream import AudioPiped
from pytgcalls.exceptions import NoActiveGroupCall

import config
import db
import music_queue as q
import progress
import botstate
from clients import bot, assistant, call_py, LOGGER, START_TIME
from youtube import search_track, get_stream_url, get_related_track
from helpers import (
    smallcaps_title,
    random_processing_text,
    fancy_italic,
    duration_to_seconds,
    format_uptime,
    expandable_quote,
    strip_quotes,
    smallcaps,
    DIVIDER,
    bullet_lines,
)

from nowplaying import generate_now_playing_card
from assistant_join import ensure_assistant_in_chat

OWNER_FILTER = filters.user(config.OWNER_ID) if config.OWNER_ID else filters.create(lambda _, __, ___: False)

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

# Short alias for the premium small-caps title style
T = smallcaps_title

# After /addvd, wait for the owner's next image/video/gif (private start message)
_pending_addvd = set()

# After /addvd2, wait for the owner's next image/video/gif (GROUP start message)
_pending_addvd2 = set()

_SEND_MEDIA_MAP_NAME = {"photo": "send_photo", "video": "send_video", "animation": "send_animation"}

# chat_id -> last few played video ids (autoplay uses this so songs don't repeat)
_played: dict = {}


# ---------------------------------------------------------------------------
# Message frame — every bot message uses the same frame as the Start / Now
# Playing messages: a header line, then the body inside an expandable quote
# that ends with the decorative divider.
# ---------------------------------------------------------------------------
def _frame(header: str, body: str, icon: str = "❖") -> str:
    return f"{icon} {T(header)}..!! ✦\n\n" + expandable_quote(f"{body}\n\n{DIVIDER}")


# ---------------------------------------------------------------------------
# Peer cache helper — avoids the "Peer id invalid" error.
# When the assistant account receives no direct updates in a chat (it only
# joins the VC), pyrogram cannot cache its peer/access_hash and later
# change_stream/leave_group_call can fail. On such an error we refresh the
# dialogs once and retry.
# ---------------------------------------------------------------------------
async def _refresh_assistant_peers():
    try:
        async for _ in assistant.get_dialogs():
            pass
    except Exception as e:
        LOGGER.warning(f"Peer refresh fail: {e}")


def _is_peer_error(e: Exception) -> bool:
    return isinstance(e, ValueError) and "Peer id invalid" in str(e)


# ---------------------------------------------------------------------------
# Admin / owner check — /reload is for group admins or the bot OWNER_ID only.
# ---------------------------------------------------------------------------
async def _is_group_admin(client, chat_id: int, user_id: int) -> bool:
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ADMIN_STATUSES
    except Exception:
        return False


ADMIN_ONLY_FRAMED = _frame(
    "Access Denied",
    f"⌾ {T('Only group admins or the owner can use this command')}.",
    "❌",
)

# Plain version — used for button pop-up alerts (alerts cannot hold a frame)
NOT_YOUR_REQUEST_TEXT = (
    f"❌ {T('This is not your request')}!\n"
    f"{T('Only the person who requested this song, or a group admin / owner, can control it')}."
)

NOT_YOUR_REQUEST_FRAMED = _frame(
    "Access Denied",
    f"⌾ {T('This is not your request')}!\n"
    f"╰┈➤ {T('Only the person who requested this song, or a group admin / owner, can control it')}.",
    "❌",
)


# ---------------------------------------------------------------------------
# Control-permission check — /skip /pause /resume /stop (and their inline
# buttons) can be used by only 3 kinds of people: the requester of the
# current track, a group admin, or the bot owner. Everyone else sees
# NOT_YOUR_REQUEST_TEXT.
# ---------------------------------------------------------------------------
async def _can_control(client, chat_id: int, user_id: int) -> bool:
    if await _is_group_admin(client, chat_id, user_id):
        return True
    track = q.get_now_playing(chat_id)
    return bool(track and track.get("requested_by_id") == user_id)


ASSISTANT_NOT_JOINED_TEXT = _frame(
    "Assistant Missing",
    f"⌾ {T('My assistant account is not in this group')}!\n"
    f"⌾ {T('The assistant account must be in the group to play music')}.\n\n"
    f"╰┈➤ {T('Add')} @{config.ASSISTANT_USERNAME} {T('to the group, or let it join')}.\n"
    f"╰┈➤ {T('Then send')} `/play` {T('again')}.",
    "❌",
)

ASSISTANT_FLOOD_TEXT = _frame(
    "Rate Limited",
    f"⌾ {T('Telegram has applied a temporary rate limit, please try again in a little while')}.",
    "⏳",
)


# ---------------------------------------------------------------------------
# Safe send / edit helpers (expandable quote with plain-text fallback)
# ---------------------------------------------------------------------------
async def _safe_quote_send(action, text: str):
    """
    Sends/edits a message with an expandable quote. If your pyrogram/kurigram
    build does not support `<blockquote expandable>`, the same message goes
    out as plain text (no quote) without crashing — the text style stays
    exactly the same.
    """
    try:
        return await action(text)
    except Exception as e:
        LOGGER.warning(f"Quote send fail, plain fallback: {e}")
        return await action(strip_quotes(text))


async def _reply(message, text: str, **kwargs):
    """reply_text with the expandable-quote fallback."""
    return await _safe_quote_send(lambda t: message.reply_text(t, **kwargs), text)


async def _edit(message, text: str, **kwargs):
    """edit_text with the expandable-quote fallback."""
    return await _safe_quote_send(lambda t: message.edit_text(t, **kwargs), text)


# ---------------------------------------------------------------------------
# Owner: /on /off — global switch to turn the whole bot on/off.
# While OFF the bot answers no message/button, only /on /off keep working.
# Persisted in the DB, so the status survives restarts.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("on") & OWNER_FILTER)
async def on_command(client, message: Message):
    botstate.set_enabled(True)
    await db.set_bot_status(True)
    await _reply(message, _frame("Bot Status", f"⌾ {T('The bot has been turned on')}.", "✅"))


@bot.on_message(filters.command("off") & OWNER_FILTER)
async def off_command(client, message: Message):
    botstate.set_enabled(False)
    await db.set_bot_status(False)
    await _reply(
        message,
        _frame(
            "Bot Status",
            f"⌾ {T('The bot has been turned off')}.\n"
            f"╰┈➤ {T('Now only')} `/on` {T('will work')}.",
            "🔴",
        ),
    )


def _off_blocker(_, __, message: Message) -> bool:
    if botstate.is_enabled():
        return False
    text = message.text or message.caption or ""
    # /on and /off must always work, even while the bot is OFF
    return not text.startswith(("/on", "/off"))


def _off_blocker_cb(_, __, cq: CallbackQuery) -> bool:
    return not botstate.is_enabled()


# group=-1 -> this handler runs first; while OFF, no other handler ever
# receives the message/callback (StopPropagation).
@bot.on_message(filters.create(_off_blocker), group=-1)
async def _blocked_while_off(client, message: Message):
    raise StopPropagation


@bot.on_callback_query(filters.create(_off_blocker_cb), group=-1)
async def _blocked_cb_while_off(client, cq: CallbackQuery):
    await cq.answer(T("The bot is currently off"), show_alert=True)
    raise StopPropagation


def _btn(text: str, *, style: str = None, **kwargs) -> InlineKeyboardButton:
    """
    Builds an InlineKeyboardButton. Telegram Bot API 9.4 colored buttons
    (style="primary"/"success"/"danger") only show up if your
    pyrogram/kurigram/pyrofork library supports them — if not, a normal
    (colorless) button is created without any error.
    """
    if style:
        try:
            return InlineKeyboardButton(text, style=style, **kwargs)
        except TypeError:
            pass
    return InlineKeyboardButton(text, **kwargs)


def _controls_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                _btn("▶️", callback_data="m_resume", style="success"),
                _btn("⏸", callback_data="m_pause", style="primary"),
                _btn("🔁", callback_data="m_replay", style="primary"),
                _btn("⏭", callback_data="m_skip", style="primary"),
                _btn("⏹", callback_data="m_stop", style="danger"),
            ],
            [_btn(f"🔄 {T('autoplay')}", callback_data="m_autoplay", style="primary")],
            [_btn(f"⊙ {T('close')} ⊙", callback_data="m_close", style="danger")],
        ]
    )





def _start_keyboard(bot_username: str):
    return InlineKeyboardMarkup(
        [
            [
                _btn(
                    f"➕ {T('add me to your group')}",
                    url=f"https://t.me/{bot_username}?startgroup=true",
                    style="success",
                )
            ],
            [
                _btn(f"👑 {T('owner')}", url=config.OWNER_URL, style="primary"),
                _btn(f"🛠 {T('support')}", url=config.SUPPORT_URL, style="primary"),
            ],
            [
                _btn(f"📢 {T('channel')}", url=config.CHANNEL_URL, style="primary"),
                _btn(f"❓ {T('help')}", callback_data="help_menu", style="primary"),
            ],
        ]
    )


def _help_keyboard():
    return InlineKeyboardMarkup(
        [[_btn(f"🔙 {T('back')}", callback_data="back_to_start", style="primary")]]
    )


async def _send_welcome(chat_id: int, text: str, reply_markup, media: dict = None):
    """Sends the welcome message with the given media (photo/video/gif) or text only."""
    if media:
        send_func = getattr(bot, _SEND_MEDIA_MAP_NAME.get(media["media_type"], "send_photo"))
        try:
            return await _safe_quote_send(
                lambda t: send_func(chat_id, media["file_id"], caption=t, reply_markup=reply_markup),
                text,
            )
        except Exception as e:
            LOGGER.warning(f"Start media send fail, text fallback: {e}")
    return await _safe_quote_send(
        lambda t: bot.send_message(chat_id, t, reply_markup=reply_markup, disable_web_page_preview=True),
        text,
    )


async def _edit_body(cq_message, text: str, reply_markup):
    """Edits the message on a callback — whether it is a media caption or plain text."""
    if cq_message.photo or cq_message.video or cq_message.animation:
        await _safe_quote_send(lambda t: cq_message.edit_caption(t, reply_markup=reply_markup), text)
    else:
        await _safe_quote_send(
            lambda t: cq_message.edit_text(t, reply_markup=reply_markup, disable_web_page_preview=True),
            text,
        )



HELP_TEXT = _frame(
    "Available Commands",
    f"╰┈➤ `/play [song]` — {T('play a song or add it to the queue')}\n"
    f"╰┈➤ `/skip` — {T('play the next song')} ({T('admin / requester')})\n"
    f"╰┈➤ `/pause` — {T('pause the music')} ({T('admin / requester')})\n"
    f"╰┈➤ `/resume` — {T('resume the music')} ({T('admin / requester')})\n"
    f"╰┈➤ `/stop` — {T('stop the music')} ({T('admin / requester')})\n"
    f"╰┈➤ `/autoplay` — {T('keep related songs playing')} ({T('admin only')})\n"
    f"╰┈➤ `/reload` — {T('refresh the bot')} ({T('admin only')})\n"
    f"╰┈➤ `/id` — {T('show your / the group ID')}",
    "🦋",
)


def _welcome_text(user_name: str, user_id: int, bot_name: str, bot_username: str) -> str:
    user_tag = f"[{T(user_name)}](tg://user?id={user_id})"
    bot_tag = f"[{fancy_italic(bot_name)}](https://t.me/{bot_username})"
    # The body goes inside an expandable quote — tap to expand and scroll
    # inside it. Text style stays the same.
    body = (
        f"🦋 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ {bot_tag}\n"
        f"『 ᴘʀᴇᴍɪᴜᴍ ✧ ᴀᴅ-ꜰʀᴇᴇ ✧ ᴜʟᴛʀᴀ sᴍᴏᴏᴛʜ 』\n\n"
        f"🦋 ʜɪɢʜ • Qᴜᴀʟɪᴛʏ • ᴍᴜsɪᴄ • ʙᴏᴛ\n"
        f"ғᴏʀ ᴛᴇʟᴇɢʀᴀᴍ ɢʀᴏᴜᴘs & ᴄʜᴀɴɴᴇʟs\n\n"
        f"🦋✦ ɪɴsᴛᴀɴᴛ sᴛʀᴇᴀᴍɪɴɢ\n"
        f"🦋✦ ᴜʟᴛʀᴀ sᴍᴏᴏᴛʜ ᴘʟᴀʏʙᴀᴄᴋ\n"
        f"🦋✦ ᴄʀʏsᴛᴀʟ ᴄʟᴇᴀʀ sᴏᴜɴᴅ • ɴᴏ ʟᴀɢ\n\n"
        f"🦋✦ ᴛᴀᴘ ʜᴇʟᴘ ᴛᴏ ᴠɪᴇᴡ ᴀʟʟ ᴄᴏᴍᴍᴀɴᴅs\n\n"
        f"🦋ᴘᴏᴡᴇʀᴇᴅ ʙʏ : [Aᴅɪᴛʏᴀ × Aᴘɪꜱ](https://t.me/AdityaXzexxyAPI)\n\n"
        f"╭───────────── ✦ ─────────────╮\n"
        f"❖ ᴇɴᴊᴏʏ ᴛʜᴇ ᴍᴜsɪᴄ ❖\n"
        f"╰───────────── ✦ ─────────────╯"
    )
    return f"🦋 ʜᴇʏ {user_tag}..!! ✦\n\n" + expandable_quote(body)



def _group_start_text(bot_name: str) -> str:
    """Group /start message — with quick info about what the music bot does."""
    return f"✨ {fancy_italic(bot_name)} ɪs ᴏɴʟɪɴᴇ ᴀɴᴅ ʀᴇᴀᴅʏ ✨\n\n" + expandable_quote(
        "🎧 ᴍᴜsɪᴄ ᴘᴀɴᴇʟ\n"
        f"╰┈➤ /play [sᴏɴɢ ɴᴀᴍᴇ] — {T('play a song')}\n"
        "╰┈➤ /skip • /pause • /resume • /stop\n"
        f"╰┈➤ /autoplay — {T('keep related songs playing')}\n"
        f"╰┈➤ /queue — {T('list of upcoming songs')}\n\n"
        "⌾ ᴄᴏɴᴛʀᴏʟ : ᴀᴅᴍɪɴs ᴀɴᴅ ʀᴇǫᴜᴇsᴛᴇʀ ᴏɴʟʏ\n"
        "⌾ ǫᴜᴀʟɪᴛʏ : ʜɪɢʜ ᴅᴇғɪɴɪᴛɪᴏɴ ᴀᴜᴅɪᴏ\n\n"
        "•── ⋅ ⋅  ────── ⋅᯽⋅ ────── ⋅ ⋅ ⋅──•"
    )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("start"))
async def start_cmd(client, message: Message):
    await db.add_user(message.from_user.id)
    me = await bot.get_me()

    if message.chat.type != "private":
        # /start in a group — separate message, separate media (set via /addvd2)
        await db.add_chat(message.chat.id)
        media = await db.get_group_start_media()
        await _send_welcome(
            message.chat.id,
            _group_start_text(me.first_name),
            _start_keyboard(me.username),
            media,
        )
    else:
        # Private /start — the welcome message, media set via /addvd
        media = await db.get_start_media()
        await _send_welcome(
            message.chat.id,
            _welcome_text(message.from_user.first_name, message.from_user.id, me.first_name, me.username),
            _start_keyboard(me.username),
            media,
        )

    # Tell the owner who used the bot (in private chat)
    if message.chat.type == "private" and config.OWNER_ID and message.from_user.id != config.OWNER_ID:
        try:
            owner_text = _frame(
                "Bot Used",
                f"⌾ {T('Name')} : {message.from_user.first_name}\n"
                f"⌾ {T('Username')} : @{message.from_user.username}\n"
                f"⌾ {T('ID')} : `{message.from_user.id}`",
                "👤",
            )
            await _safe_quote_send(lambda t: bot.send_message(config.OWNER_ID, t), owner_text)
        except Exception as e:
            LOGGER.warning(f"Owner notify fail: {e}")


@bot.on_callback_query(filters.regex("^help_menu$"))
async def help_menu_cb(client, cq: CallbackQuery):
    await cq.answer()
    await _edit_body(cq.message, HELP_TEXT, _help_keyboard())


@bot.on_callback_query(filters.regex("^back_to_start$"))
async def back_to_start_cb(client, cq: CallbackQuery):
    await cq.answer()
    me = await bot.get_me()
    if cq.message.chat.type != "private":
        text = _group_start_text(me.first_name)
    else:
        text = _welcome_text(cq.from_user.first_name, cq.from_user.id, me.first_name, me.username)
    await _edit_body(cq.message, text, _start_keyboard(me.username))


# ---------------------------------------------------------------------------
# Bot added to a group
# ---------------------------------------------------------------------------
@bot.on_message(filters.new_chat_members)
async def added_to_group(client, message: Message):
    me = await bot.get_me()
    if not any(u.id == me.id for u in message.new_chat_members):
        return

    await db.add_chat(message.chat.id)
    adder = message.from_user.first_name if message.from_user else "there"

    await _reply(
        message,
        f"🎉 ʜᴇʏ **{adder}**..!! ✦\n\n"
        + expandable_quote(
            f"ᴛʜᴀɴᴋ ʏᴏᴜ ғᴏʀ ᴀᴅᴅɪɴɢ **[{me.first_name}](https://t.me/{me.username})** ɪɴ {message.chat.title}.\n\n"
            f"🎶 **{me.first_name}** ɪs ɴᴏᴡ ʀᴇᴀᴅʏ ᴛᴏ sᴛʀᴇᴀᴍ ᴍᴜsɪᴄ, ᴍᴀɴᴀɢᴇ ᴄʜᴀᴛs ᴀɴᴅ ᴅᴇʟɪᴠᴇʀ ᴛʜᴇ ʙᴇsᴛ ᴇxᴘᴇʀɪᴇɴᴄᴇ.\n\n"
            f"{DIVIDER}"
        ),
        reply_markup=_start_keyboard(me.username),
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /play — open to everyone
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("play") & filters.group)
async def play_command(client, message: Message):
    if len(message.command) < 2:
        return await _reply(
            message,
            _frame(
                "Song Name Missing",
                f"⌾ {T('Please enter a song name too')}!\n"
                f"╰┈➤ {T('Example')} : `/play Aaj Ki Raat`",
                "❌",
            ),
        )

    query = message.text.split(None, 1)[1]
    chat_id = message.chat.id
    requester = message.from_user.mention if message.from_user else "Someone"
    requester_id = message.from_user.id if message.from_user else None

    # First confirm the assistant account is in this group — without it the
    # VC cannot be joined. We also try to make it join from here.
    joined, reason = await ensure_assistant_in_chat(chat_id)
    if not joined:
        if reason == "flood_wait":
            return await _reply(message, ASSISTANT_FLOOD_TEXT)
        return await _reply(message, ASSISTANT_NOT_JOINED_TEXT)

    status = await message.reply_text(random_processing_text())

    track = await search_track(query)
    if not track:
        return await _edit(
            status,
            _frame("Not Found", f"⌾ {T('Nothing found, try a different name')}.", "❌"),
        )

    # The API prepares the song first — it can take a while, so we wait here
    # until the song is ready (max 3 minutes).
    try:
        await _edit(
            status,
            _frame("Please Wait", f"⌾ {T('Preparing your song, please wait')}...", "⏳"),
        )
    except Exception:
        pass

    try:
        stream_url = await get_stream_url(track["id"])
    except Exception as e:
        LOGGER.error(f"Stream URL error: {e}")
        return await _edit(
            status,
            _frame(
                "Download Error",
                f"⌾ {T('The song could not be loaded, please try again later or request another song')}.",
                "❌",
            ),
        )


    track["stream_url"] = stream_url
    track["requested_by"] = requester
    track["requested_by_id"] = requester_id

    # If something is already playing -> add to the queue
    if q.is_playing(chat_id):
        position = q.push(chat_id, track)
        await status.delete()
        await _reply(
            message,
            _frame(
                "Added To Queue",
                f"» 『 {T(track['title'])} 』\n"
                f"⌾ {T('position')} : #{position}\n"
                f"⌾ {T('duration')} : {track['duration']} ᴍɪɴᴜᴛᴇs\n"
                f"⌾ {T('requested')} : {requester}",
                "🎵",
            ),
        )
        return

    await status.delete()
    await _start_playing(chat_id, track, message)


async def _start_playing(chat_id: int, track: dict, message: Message):
    """Joins/changes the VC, plays the track and sends the Now Playing card."""
    try:
        try:
            await call_py.join_group_call(chat_id, AudioPiped(track["stream_url"]))
        except NoActiveGroupCall:
            return await _reply(
                message,
                _frame(
                    "Voice Chat Not Active",
                    f"⌾ {T('Please start a voice chat in the group first')}.\n"
                    f"╰┈➤ {T('Group Settings → Voice Chat → Start Voice Chat')}\n\n"
                    f"{T('Then send')} `/play` {T('again')}.",
                    "❌",
                ),
            )
        except Exception as e:
            if _is_peer_error(e):
                await _refresh_assistant_peers()
            try:
                await call_py.change_stream(chat_id, AudioPiped(track["stream_url"]))
            except Exception as e2:
                LOGGER.error(f"Play error: {e2}")
                return await _reply(
                    message,
                    _frame(
                        "Play Failed",
                        f"⌾ {T('Could not play the song')}.\n"
                        f"╰┈➤ {T('Please check that the voice chat is active, then try again')}.",
                        "❌",
                    ),
                )

        q.set_now_playing(chat_id, track)
        await _send_now_playing(chat_id, track, message)

    except Exception as e:
        LOGGER.error(f"_start_playing fatal error: {e}")
        await _reply(
            message,
            _frame("Error", f"⌾ {T('Something went wrong, please try again')}.", "❌"),
        )


def _now_playing_caption(track: dict) -> str:
    # Screenshot-style fancy frame + expandable quote (tap to expand/scroll).
    artists = [a.strip() for a in str(track.get("channel") or "").replace("-", ",").split(",") if a.strip()]
    if not artists:
        artists = [track.get("requested_by", "Unknown")]

    body = (
        f"» 『 {T(track['title'])} 』\n"
        f"{bullet_lines(T(a) for a in artists)}\n\n"
        f"⌾ {T('duration')} : {track['duration']}\n"
        f"⌾ {T('by')} : {track.get('requested_by', 'Unknown')}\n\n"
        f"{DIVIDER}"
    )
    return f"❖ {T('Now Playing')}..!! ✦\n\n" + expandable_quote(body)


async def _send_now_playing(chat_id: int, track: dict, message: Message = None, edit_message: Message = None):
    """
    Sends the Now Playing card. If `edit_message` is given (e.g. from the
    skip button), that message is updated in place instead of sending a new
    one and deleting the old one — the card never "disappears", it just
    refreshes itself.
    """
    # Remember what was played (autoplay avoids repeating these)
    _played.setdefault(chat_id, []).append(track["id"])
    del _played[chat_id][:-15]

    caption = _now_playing_caption(track)
    card = await generate_now_playing_card(track.get("thumbnail"), track["title"], track["duration"])
    markup = _controls_keyboard()
    media = card or track.get("thumbnail")

    sent = None

    if edit_message is not None:
        try:
            if media:
                sent = await edit_message.edit_media(InputMediaPhoto(media, caption=caption), reply_markup=markup)
            else:
                sent = await edit_message.edit_text(caption, reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            LOGGER.warning(f"Now playing in-place edit fail, sending a new message: {e}")

    if sent is None:
        try:
            if media:
                sent = await _safe_quote_send(
                    lambda t: bot.send_photo(chat_id, media, caption=t, reply_markup=markup), caption
                )
            elif message is not None:
                sent = await _safe_quote_send(
                    lambda t: message.reply_text(t, reply_markup=markup, disable_web_page_preview=True), caption
                )
            else:
                sent = await _safe_quote_send(
                    lambda t: bot.send_message(chat_id, t, reply_markup=markup, disable_web_page_preview=True),
                    caption,
                )
        except Exception as e:
            LOGGER.warning(f"Now playing card send fail: {e}")
            plain = strip_quotes(caption)
            if message is not None:
                sent = await message.reply_text(plain, reply_markup=markup, disable_web_page_preview=True)
            else:
                sent = await bot.send_message(chat_id, plain, reply_markup=markup, disable_web_page_preview=True)


    # 🎚️ Start the live progress bar — it advances by itself from 00:00 up
    # to the duration, along with the song.
    if sent is not None:
        total_sec = duration_to_seconds(track.get("duration"))
        progress.start(chat_id, track["id"])
        progress.start_updater(
            chat_id, sent,
            lambda: _now_playing_caption(track),
            _controls_keyboard,
            track["id"], total_sec,
        )

    return sent


# ---------------------------------------------------------------------------
# When the stream ends, play the next song from the queue
# ---------------------------------------------------------------------------
async def _autoplay_track(chat_id: int, last_track: dict):
    """
    Autoplay: when it is ON for this chat, finds a song related to the one
    that just finished and returns it (ready to play). Returns None if
    autoplay is off or nothing could be found.
    """
    try:
        if not await db.get_autoplay(chat_id):
            return None
        exclude = list(_played.get(chat_id, []))
        if last_track.get("id"):
            exclude.append(last_track["id"])
        related = await get_related_track(last_track.get("title"), exclude_id=exclude)
        if not related:
            return None
        related["stream_url"] = await get_stream_url(related["id"])
        related["requested_by"] = T("Autoplay")
        related["requested_by_id"] = None
        return related
    except Exception as e:
        LOGGER.warning(f"Autoplay error: {e}")
        return None


async def _next_or_autoplay(chat_id: int):
    """Next queued track; if the queue is empty, an autoplay track (when autoplay is ON)."""
    next_track = q.pop_next(chat_id)
    if next_track:
        return next_track
    last = q.get_now_playing(chat_id)
    if last:
        return await _autoplay_track(chat_id, last)
    return None


@call_py.on_stream_end()
async def on_stream_end(client, update):
    chat_id = update.chat_id
    next_track = await _next_or_autoplay(chat_id)

    if not next_track:
        q.set_now_playing(chat_id, None)
        progress.clear(chat_id)
        try:
            await call_py.leave_group_call(chat_id)
        except Exception as e:
            LOGGER.warning(f"Auto leave fail: {e}")
        return


    try:
        try:
            await call_py.change_stream(chat_id, AudioPiped(next_track["stream_url"]))
        except Exception as e:
            if _is_peer_error(e):
                await _refresh_assistant_peers()
            await call_py.join_group_call(chat_id, AudioPiped(next_track["stream_url"]))

        q.set_now_playing(chat_id, next_track)
        await _send_now_playing(chat_id, next_track)
    except Exception as e:
        LOGGER.error(f"Auto-play next error: {e}")


# ---------------------------------------------------------------------------
# /skip /pause /resume /stop — group admin / owner / requester
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("skip") & filters.group)
async def skip_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await _reply(message, NOT_YOUR_REQUEST_FRAMED)

    chat_id = message.chat.id
    next_track = await _next_or_autoplay(chat_id)
    if not next_track:
        q.set_now_playing(chat_id, None)
        progress.clear(chat_id)
        try:
            await call_py.leave_group_call(chat_id)
        except Exception:
            pass
        return await _reply(
            message,
            _frame("Queue Empty", f"⌾ {T('The queue is empty, left the voice chat')}.", "⏭"),
        )

    try:
        await call_py.change_stream(chat_id, AudioPiped(next_track["stream_url"]))
    except Exception as e:
        if _is_peer_error(e):
            await _refresh_assistant_peers()
        try:
            await call_py.join_group_call(chat_id, AudioPiped(next_track["stream_url"]))
        except Exception as e2:
            LOGGER.error(f"Skip error: {e2}")
            return await _reply(
                message,
                _frame("Skip Failed", f"⌾ {T('Could not skip, please try again')}.", "❌"),
            )

    q.set_now_playing(chat_id, next_track)
    await _send_now_playing(chat_id, next_track, message)


@bot.on_message(filters.command("pause") & filters.group)
async def pause_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await _reply(message, NOT_YOUR_REQUEST_FRAMED)
    try:
        await call_py.pause_stream(message.chat.id)
        q.set_state(message.chat.id, "paused")
        progress.pause(message.chat.id)
        await _reply(message, _frame("Paused", f"⌾ {T('The music has been paused')}.", "⏸"))
    except Exception as e:
        LOGGER.warning(f"Pause error: {e}")
        await _reply(message, _frame("Error", f"⌾ {T('Nothing is playing right now')}.", "❌"))


@bot.on_message(filters.command("resume") & filters.group)
async def resume_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await _reply(message, NOT_YOUR_REQUEST_FRAMED)
    try:
        await call_py.resume_stream(message.chat.id)
        q.set_state(message.chat.id, "playing")
        progress.resume(message.chat.id)
        await _reply(message, _frame("Resumed", f"⌾ {T('The music has been resumed')}.", "▶️"))
    except Exception as e:
        LOGGER.warning(f"Resume error: {e}")
        await _reply(message, _frame("Error", f"⌾ {T('Nothing is playing right now')}.", "❌"))


@bot.on_message(filters.command(["stop", "end"]) & filters.group)
async def stop_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await _reply(message, NOT_YOUR_REQUEST_FRAMED)
    try:
        await call_py.leave_group_call(message.chat.id)
    except Exception:
        pass
    q.clear(message.chat.id)
    progress.clear(message.chat.id)
    await _reply(message, _frame("Stopped", f"⌾ {T('Voice chat closed')}.", "⏹️"))


# ---------------------------------------------------------------------------
# /reload — group admin / owner only. Checks whether the bot itself is admin.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("autoplay") & filters.group)
async def autoplay_command(client, message: Message):
    if not message.from_user or not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await _reply(message, ADMIN_ONLY_FRAMED)

    chat_id = message.chat.id
    arg = message.command[1].lower() if len(message.command) > 1 else None
    if arg in ("on", "enable"):
        new_value = True
    elif arg in ("off", "disable"):
        new_value = False
    elif arg is None:
        new_value = not await db.get_autoplay(chat_id)
    else:
        return await _reply(
            message,
            _frame(
                "Autoplay",
                f"⌾ {T('Usage')} : `/autoplay on` {T('or')} `/autoplay off`",
                "❌",
            ),
        )

    await db.set_autoplay(chat_id, new_value)
    if new_value:
        body = (
            f"⌾ {T('Autoplay has been turned on')}.\n"
            f"╰┈➤ {T('When the queue ends, related songs will keep playing')}."
        )
    else:
        body = f"⌾ {T('Autoplay has been turned off')}."
    await _reply(message, _frame("Autoplay", body, "🔄"))


@bot.on_message(filters.command("reload") & filters.group)
async def reload_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await _reply(message, ADMIN_ONLY_FRAMED)

    me = await bot.get_me()
    try:
        bot_member = await client.get_chat_member(message.chat.id, me.id)
        is_bot_admin = bot_member.status in ADMIN_STATUSES
    except Exception as e:
        LOGGER.warning(f"Reload admin-check fail: {e}")
        is_bot_admin = False

    if is_bot_admin:
        await _reply(message, _frame("Reloaded", f"⌾ {T('Reloaded successfully')}.", "✅"))
    else:
        await _reply(
            message,
            _frame(
                "Admin Needed",
                f"⌾ {T('Please make me a group admin first, then use')} `/reload` {T('again')}.",
                "❌",
            ),
        )


# ---------------------------------------------------------------------------
# Inline buttons (below the Now Playing card)
# ---------------------------------------------------------------------------

@bot.on_callback_query(filters.regex("^m_"))
async def controls_callback(client, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    action = cq.data

    # Skip/pause/resume/stop buttons — same permission check as the commands:
    # only the requester of the current track, or a group admin/owner.
    if action in ("m_resume", "m_pause", "m_skip", "m_stop"):
        if not await _can_control(client, chat_id, cq.from_user.id):
            return await cq.answer(NOT_YOUR_REQUEST_TEXT, show_alert=True)


    try:
        if action == "m_resume":
            await call_py.resume_stream(chat_id)
            q.set_state(chat_id, "playing")
            progress.resume(chat_id)
            await cq.answer(f"▶️ {T('Resumed')}")

        elif action == "m_pause":
            await call_py.pause_stream(chat_id)
            q.set_state(chat_id, "paused")
            progress.pause(chat_id)
            await cq.answer(f"⏸ {T('Paused')}")

        elif action == "m_replay":
            track = q.get_now_playing(chat_id)
            if track:
                await call_py.change_stream(chat_id, AudioPiped(track["stream_url"]))
                progress.replay(chat_id)
                await cq.answer(f"🔁 {T('Replaying')}")
            else:
                await cq.answer(T("Nothing is playing right now"), show_alert=True)

        elif action == "m_skip":
            await cq.answer(f"⏭ {T('Skipping')}")
            next_track = await _next_or_autoplay(chat_id)
            if not next_track:
                q.set_now_playing(chat_id, None)
                progress.clear(chat_id)
                await call_py.leave_group_call(chat_id)
                try:
                    await cq.message.edit_reply_markup(None)
                except Exception:
                    pass
                await _reply(
                    cq.message,
                    _frame("Queue Empty", f"⌾ {T('The queue is empty, left the voice chat')}.", "⏭"),
                )
            else:
                await call_py.change_stream(chat_id, AudioPiped(next_track["stream_url"]))
                q.set_now_playing(chat_id, next_track)
                # Instead of sending a new message, the same card is updated in place
                # (_send_now_playing starts the new track's progress bar itself)
                await _send_now_playing(chat_id, next_track, edit_message=cq.message)

        elif action == "m_stop":
            await call_py.leave_group_call(chat_id)
            q.clear(chat_id)
            progress.clear(chat_id)
            await cq.answer(f"⏹ {T('Stopped')}")
            try:
                await cq.message.edit_reply_markup(None)
            except Exception:
                pass
            await _reply(cq.message, _frame("Stopped", f"⌾ {T('Voice chat closed')}.", "⏹️"))

        elif action == "m_autoplay":
            if not await _is_group_admin(client, chat_id, cq.from_user.id):
                return await cq.answer(
                    f"❌ {T('Only group admins or the owner can use this command')}.", show_alert=True
                )
            new_value = not await db.get_autoplay(chat_id)
            await db.set_autoplay(chat_id, new_value)
            await cq.answer(f"🔄 {T('Autoplay turned on') if new_value else T('Autoplay turned off')}")

        elif action == "m_close":

            await cq.answer()
            progress.cancel_task(chat_id)
            await cq.message.delete()

    except Exception as e:
        LOGGER.warning(f"Callback error ({action}): {e}")
        await cq.answer(f"❌ {T('Something went wrong, please try again')}", show_alert=True)


# ---------------------------------------------------------------------------
# Owner: /addvd /delvd — image/video/gif sent along with the /start message
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("addvd") & OWNER_FILTER)
async def addvd_command(client, message: Message):
    _pending_addvd.add(message.from_user.id)
    await _reply(
        message,
        _frame(
            "Start Media",
            f"⌾ {T('Now send an image, video or gif')}.\n"
            f"╰┈➤ {T('It will be sent with the PRIVATE start message to everyone from now on')}.",
            "🖼",
        ),
    )


@bot.on_message(filters.command("delvd") & OWNER_FILTER)
async def delvd_command(client, message: Message):
    await db.delete_start_media()
    _pending_addvd.discard(message.from_user.id)
    await _reply(
        message,
        _frame("Start Media", f"⌾ {T('Private start message media removed')}.", "🗑"),
    )


@bot.on_message(filters.command("addvd2") & OWNER_FILTER)
async def addvd2_command(client, message: Message):
    _pending_addvd2.add(message.from_user.id)
    await _reply(
        message,
        _frame(
            "Start Media",
            f"⌾ {T('Now send an image, video or gif')}.\n"
            f"╰┈➤ {T('It will be sent with the GROUP start message to everyone from now on')}.",
            "🖼",
        ),
    )


@bot.on_message(filters.command("delvd2") & OWNER_FILTER)
async def delvd2_command(client, message: Message):
    await db.delete_group_start_media()
    _pending_addvd2.discard(message.from_user.id)
    await _reply(
        message,
        _frame("Start Media", f"⌾ {T('Group start message media removed')}.", "🗑"),
    )


@bot.on_message(
    (filters.photo | filters.video | filters.animation)
    & OWNER_FILTER
    & filters.create(
        lambda _, __, m: bool(m.from_user)
        and (m.from_user.id in _pending_addvd or m.from_user.id in _pending_addvd2)
    )
)
async def addvd_receive(client, message: Message):
    is_group_variant = message.from_user.id in _pending_addvd2
    _pending_addvd.discard(message.from_user.id)
    _pending_addvd2.discard(message.from_user.id)

    if message.photo:
        file_id, media_type = message.photo.file_id, "photo"
    elif message.video:
        file_id, media_type = message.video.file_id, "video"
    elif message.animation:
        file_id, media_type = message.animation.file_id, "animation"
    else:
        return

    if is_group_variant:
        await db.set_group_start_media(file_id, media_type)
        await _reply(
            message,
            _frame("Start Media", f"⌾ {T('Group start message media has been set')}.", "✅"),
        )
    else:
        await db.set_start_media(file_id, media_type)
        await _reply(
            message,
            _frame("Start Media", f"⌾ {T('Private start message media has been set')}.", "✅"),
        )


# ---------------------------------------------------------------------------
# Owner: /broadcast
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("broadcast") & OWNER_FILTER)
async def broadcast_command(client, message: Message):
    if len(message.command) < 2 and not message.reply_to_message:
        return await _reply(
            message,
            _frame(
                "Broadcast",
                f"⌾ {T('Please provide a message to broadcast')}!\n"
                f"╰┈➤ {T('Example')} : `/broadcast Hello everyone`",
                "❌",
            ),
        )

    text = message.text.split(None, 1)[1] if len(message.command) > 1 else None
    users = await db.get_all_users()
    status = await _reply(
        message,
        _frame("Broadcast", f"⌾ {T('broadcasting to')} {len(users)} {T('users')}...", "📢"),
    )

    sent, failed = 0, 0
    for uid in users:
        try:
            if message.reply_to_message:
                await message.reply_to_message.copy(uid)
            else:
                await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1

    await _edit(
        status,
        _frame(
            "Broadcast Done",
            f"⌾ {T('sent')} : {sent}\n⌾ {T('failed')} : {failed}",
            "✅",
        ),
    )


# ---------------------------------------------------------------------------
# /id — show user and chat id
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("id"))
async def id_command(client, message: Message):
    user_id = message.from_user.id if message.from_user else "Unknown"
    lines = [f"⌾ {T('your id')} : `{user_id}`"]
    if message.chat.type != "private":
        lines.append(f"⌾ {T('chat id')} : `{message.chat.id}`")
    if message.reply_to_message and message.reply_to_message.from_user:
        lines.append(f"⌾ {T('replied user id')} : `{message.reply_to_message.from_user.id}`")
    await _reply(message, _frame("ID Info", "\n".join(lines), "👤"))
