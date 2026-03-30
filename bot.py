import asyncio
import functools
import html as html_lib
import json
import logging
import os
import warnings
import anthropic
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)
from names import extract_names, reflow_paragraphs
from lookup import lookup_all, search_twitter, search_instagram, twitter_search_url, SerperCreditsError
from formatter import apply_substitutions, format_platform

warnings.filterwarnings("ignore", message="If 'per_message=False'", category=UserWarning)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_data_dir = os.environ.get("DATA_DIR", os.path.dirname(__file__))
CONFIG_PATH = os.path.join(_data_dir, "config.json")

_SAMPLE_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.sample.json")

ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])

# ── Conversation states ────────────────────────────────────────────────────────
(
    CONFIRM_NAMES,       # 0 — main conv
    AWAIT_MANUAL_NAMES,  # 1 — main conv
    AWAIT_HANDLE_INPUT,  # 2 — main conv
    SETUP_PLATFORMS,     # 3 — setup conv
    SETUP_FIELD,         # 4 — setup conv
    SETUP_CONFIRM,       # 5 — setup conv
    MANAGE_USERS,        # 6 — users conv
    ADD_USER,            # 7 — users conv
    DELETE_USER,         # 8 — users conv
    SELECT_NAMES,        # 9 — main conv
) = range(10)


# ── Config helpers ─────────────────────────────────────────────────────────────

_config_cache: dict | None = None


def load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not os.path.exists(CONFIG_PATH):
        with open(_SAMPLE_CONFIG_PATH) as f:
            defaults = json.load(f)
        save_config(defaults)
        return defaults
    with open(CONFIG_PATH) as f:
        _config_cache = json.load(f)
    return _config_cache


def save_config(cfg: dict):
    global _config_cache
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    _config_cache = cfg


# ── Auth ───────────────────────────────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    if user_id == ADMIN_USER_ID:
        return True
    return user_id in load_config().get("allowed_users", [])


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_USER_ID:
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def authorized_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update.effective_user.id):
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


# ── Output sender ──────────────────────────────────────────────────────────────

PLATFORM_EMOJI = {
    "instagram": "📸 INSTAGRAM",
    "twitter":   "🐦 TWITTER",
    "bluesky":   "🦋 BLUESKY",
}


async def _add_paragraph_emojis(text: str) -> str:
    """Prepend a single apt emoji to each paragraph using Claude."""
    paragraphs = text.split("\n\n")
    numbered = "\n\n".join(f"[{i+1}] {p}" for i, p in enumerate(paragraphs))
    prompt = (
        "I have a text split into paragraphs, each prefixed with [N]. "
        "For each paragraph, prepend a single most apt emoji that captures its theme. "
        "Return only the paragraphs in the same order, each starting with the emoji "
        "followed by a space and the original paragraph text, separated by double newlines. "
        "Do not include the [N] prefix or any other text.\n\n"
        + numbered
    )
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def send_formatted_output(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("editing_handle", None)
    text = context.user_data["original_text"]
    substitutions = context.user_data.get("substitutions", {})
    config = load_config()

    if config.get("auto_paragraph", False):
        text = reflow_paragraphs(text)
        if config.get("auto_emoji", False):
            try:
                text = await _add_paragraph_emojis(text)
            except anthropic.APIStatusError as e:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"Anthropic API error — auto-emoji skipped: {e.message}",
                )

    platform_texts = apply_substitutions(text, substitutions)

    for platform in ["instagram", "twitter", "bluesky"]:
        if not config.get(platform, {}).get("enabled", True):
            continue
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=PLATFORM_EMOJI[platform],
        )
        chunks = format_platform(platform_texts[platform], platform, config)
        for chunk in chunks:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=chunk,
            )


# ── Name confirmation ──────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html_lib.escape(str(text))


def build_name_message(lookup: dict, enabled_platforms: set) -> str:
    name = lookup["name"]
    lines = [f"🔍 <b>{_esc(name)}</b>\n"]

    if "bluesky" in enabled_platforms:
        lines.append("🦋 <b>Bluesky</b> — pick one:")
        if lookup["bluesky"]:
            for i, actor in enumerate(lookup["bluesky"]):
                lines.append(f"  {i+1}. <a href=\"{_esc(actor['url'])}\">{_esc(actor['handle'])}</a> · {_esc(actor['display_name'])}")
        else:
            lines.append("  <i>No results found</i>")

    if "twitter" in enabled_platforms:
        lines.append("\n🐦 <b>Twitter</b>")
        if lookup["twitter"]:
            lines.append(f"  {_esc(lookup['twitter'])}")
        else:
            lines.append("  <i>No results found</i>")

    if "instagram" in enabled_platforms:
        lines.append("\n📸 <b>Instagram</b>")
        if lookup["instagram"]:
            lines.append(f"  {_esc(lookup['instagram'])}")
        else:
            lines.append("  <i>No results found</i>")

    return "\n".join(lines)


def build_name_keyboard(lookup: dict, name_idx: int, enabled_platforms: set, resolved: dict | None = None) -> InlineKeyboardMarkup:
    resolved = resolved or {}
    rows = []

    if "bluesky" in enabled_platforms and resolved.get("bluesky", "pending") == "pending":
        for i, actor in enumerate(lookup["bluesky"]):
            rows.append([InlineKeyboardButton(
                f"{i+1}. @{actor['handle']} · {actor['display_name']}",
                callback_data=f"bsky:{name_idx}:{i}"
            )])
        rows.append([
            InlineKeyboardButton("✏️ Add", callback_data=f"bsky:{name_idx}:search"),
            InlineKeyboardButton("🦋 Skip", callback_data=f"bsky:{name_idx}:skip"),
        ])

    if "twitter" in enabled_platforms and resolved.get("twitter", "pending") == "pending":
        tw_row = []
        if lookup["twitter"]:
            tw_row.append(InlineKeyboardButton("✅ Use", callback_data=f"tw:{name_idx}:use"))
            tw_row.append(InlineKeyboardButton("✏️ Edit", callback_data=f"tw:{name_idx}:edit"))
        else:
            tw_row.append(InlineKeyboardButton("✏️ Add", callback_data=f"tw:{name_idx}:edit"))
        tw_row.append(InlineKeyboardButton("🐦 Skip", callback_data=f"tw:{name_idx}:skip"))
        rows.append(tw_row)

    if "instagram" in enabled_platforms and resolved.get("instagram", "pending") == "pending":
        ig_row = []
        if lookup["instagram"]:
            ig_row.append(InlineKeyboardButton("✅ Use", callback_data=f"ig:{name_idx}:use"))
            ig_row.append(InlineKeyboardButton("✏️ Edit", callback_data=f"ig:{name_idx}:edit"))
        else:
            ig_row.append(InlineKeyboardButton("✏️ Add", callback_data=f"ig:{name_idx}:edit"))
        ig_row.append(InlineKeyboardButton("📸 Skip", callback_data=f"ig:{name_idx}:skip"))
        rows.append(ig_row)

    return InlineKeyboardMarkup(rows)


def is_resolved(entry: dict, enabled_platforms: set) -> bool:
    return all(entry.get(k, "pending") != "pending" for k in enabled_platforms)


async def show_next_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lookups = context.user_data["lookups"]
    idx = context.user_data.get("current_name_idx", 0)
    enabled_platforms = context.user_data.get("enabled_platforms", {"twitter", "bluesky", "instagram"})

    if idx >= len(lookups):
        await send_formatted_output(update, context)
        return ConversationHandler.END

    lookup = lookups[idx]
    context.user_data.setdefault("resolved", {})[idx] = {
        p: "pending" for p in enabled_platforms
    }

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_name_message(lookup, enabled_platforms),
        parse_mode="HTML",
        reply_markup=build_name_keyboard(lookup, idx, enabled_platforms),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    context.user_data["name_message_id"] = msg.message_id
    return CONFIRM_NAMES


async def try_advance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data["current_name_idx"]
    resolved = context.user_data["resolved"]
    enabled_platforms = context.user_data.get("enabled_platforms", {"twitter", "bluesky", "instagram"})

    if is_resolved(resolved[idx], enabled_platforms):
        lookups = context.user_data["lookups"]
        name = lookups[idx]["name"]
        r = resolved[idx]
        subs = context.user_data.setdefault("substitutions", {})
        subs[name] = {p: (r[p] if r[p] != "skip" else None) for p in enabled_platforms}
        context.user_data["current_name_idx"] = idx + 1
        return await show_next_name(update, context)

    return CONFIRM_NAMES


# ── Callback handler ───────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # No-names prompt
    if data == "detect:yes":
        context.user_data["manual_names"] = []
        await _show_manual_names_loop(query.edit_message_text, [])
        return AWAIT_MANUAL_NAMES

    if data == "detect:no":
        await query.edit_message_text("Formatting…")
        await send_formatted_output(update, context)
        return ConversationHandler.END

    # Name resolution callbacks
    parts = data.split(":")
    platform_code, name_idx, action = parts[0], int(parts[1]), parts[2]
    lookups = context.user_data["lookups"]
    lookup = lookups[name_idx]
    resolved = context.user_data["resolved"][name_idx]

    if platform_code == "bsky":
        if action == "skip":
            resolved["bluesky"] = "skip"
        elif action == "search":
            context.user_data["editing_handle"] = {"name_idx": name_idx, "platform": "bluesky"}
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Type the correct Bluesky handle (e.g. @handle.bsky.social), or search first:\n\nhttps://bsky.app",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🦋 Skip", callback_data=f"bsky:{name_idx}:skip")]]),
            )
            context.user_data["prompt_message_id"] = msg.message_id
            return AWAIT_HANDLE_INPUT
        else:
            actor = lookup["bluesky"][int(action)]
            resolved["bluesky"] = f"@{actor['handle']}"

    elif platform_code == "tw":
        if action == "skip":
            resolved["twitter"] = "skip"
        elif action == "use":
            url = lookup["twitter"]
            handle = "@" + urlparse(url).path.strip("/").split("/")[-1]
            resolved["twitter"] = handle
        elif action == "edit":
            context.user_data["editing_handle"] = {"name_idx": name_idx, "platform": "twitter"}
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Type the correct Twitter handle (e.g. @handle), or search first:\n\n{twitter_search_url(lookup['name'])}",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🐦 Skip", callback_data=f"tw:{name_idx}:skip")]]),
            )
            context.user_data["prompt_message_id"] = msg.message_id
            return AWAIT_HANDLE_INPUT

    elif platform_code == "ig":
        if action == "skip":
            resolved["instagram"] = "skip"
        elif action == "use":
            url = lookup["instagram"]
            handle = "@" + urlparse(url).path.strip("/").split("/")[-1]
            resolved["instagram"] = handle
        elif action == "edit":
            context.user_data["editing_handle"] = {"name_idx": name_idx, "platform": "instagram"}
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Type the correct Instagram handle (e.g. @handle), or search first:\n\nhttps://www.instagram.com",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📸 Skip", callback_data=f"ig:{name_idx}:skip")]]),
            )
            context.user_data["prompt_message_id"] = msg.message_id
            return AWAIT_HANDLE_INPUT

    enabled_platforms = context.user_data.get("enabled_platforms", {"twitter", "bluesky", "instagram"})
    name_message_id = context.user_data.get("name_message_id")
    if name_message_id and query.message.message_id != name_message_id:
        # Called from a prompt message — delete it and update the main name keyboard
        context.user_data.pop("editing_handle", None)
        await query.message.delete()
        await context.bot.edit_message_reply_markup(
            chat_id=update.effective_chat.id,
            message_id=name_message_id,
            reply_markup=build_name_keyboard(lookup, name_idx, enabled_platforms, resolved),
        )
    else:
        await query.edit_message_reply_markup(
            reply_markup=build_name_keyboard(lookup, name_idx, enabled_platforms, resolved)
        )
    return await try_advance(update, context)


# ── Handle input ───────────────────────────────────────────────────────────────

async def receive_handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lstrip("@")
    editing = context.user_data["editing_handle"]
    name_idx = editing["name_idx"]
    platform = editing["platform"]

    if platform == "twitter":
        url = await search_twitter(raw)
        if url:
            handle = "@" + urlparse(url).path.strip("/").split("/")[-1]
        else:
            handle = f"@{raw}"
    elif platform == "instagram":
        url = await search_instagram(raw)
        if url:
            handle = "@" + urlparse(url).path.strip("/").split("/")[-1]
        else:
            handle = f"@{raw}"
    else:
        if "." not in raw:
            raw = f"{raw}.bsky.social"
        handle = f"@{raw}"

    context.user_data["resolved"][name_idx][platform] = handle
    context.user_data.pop("editing_handle", None)

    prompt_message_id = context.user_data.pop("prompt_message_id", None)
    if prompt_message_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=update.effective_chat.id,
                message_id=prompt_message_id,
                reply_markup=None,
            )
        except Exception:
            pass

    name_message_id = context.user_data.get("name_message_id")
    lookups = context.user_data["lookups"]
    enabled_platforms = context.user_data.get("enabled_platforms", {"twitter", "bluesky", "instagram"})
    resolved = context.user_data["resolved"][name_idx]
    if name_message_id:
        await context.bot.edit_message_reply_markup(
            chat_id=update.effective_chat.id,
            message_id=name_message_id,
            reply_markup=build_name_keyboard(lookups[name_idx], name_idx, enabled_platforms, resolved),
        )

    await update.message.reply_text(f"✅ Set to {handle}.")
    return await try_advance(update, context)


# ── Manual name entry ──────────────────────────────────────────────────────────

def _build_manual_names_keyboard(names: list) -> InlineKeyboardMarkup:
    rows = []
    if names:
        rows.append([InlineKeyboardButton("🗑 Remove a name", callback_data="man:remove")])
        rows.append([InlineKeyboardButton("🔍 Look up", callback_data="man:lookup")])
    return InlineKeyboardMarkup(rows)


async def _show_manual_names_loop(send_fn, names: list):
    if names:
        display = "\n".join(f"• {_esc(n)}" for n in names)
        text = f"Names so far:\n{display}\n\nType another name to add:"
    else:
        text = "Names so far: <i>(none)</i>\n\nType a name to add:"
    await send_fn(text, parse_mode="HTML", reply_markup=_build_manual_names_keyboard(names))


async def receive_manual_names(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_names = [n.strip() for n in update.message.text.strip().splitlines() if n.strip()]
    accumulated = context.user_data.setdefault("manual_names", [])
    accumulated.extend(new_names)
    await _show_manual_names_loop(update.message.reply_text, accumulated)
    return AWAIT_MANUAL_NAMES


async def handle_manual_names_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    names = context.user_data.get("manual_names", [])

    if data == "man:lookup":
        await query.edit_message_text(f"Looking up {len(names)} name(s)…")
        enabled_platforms = context.user_data.get("enabled_platforms", {"twitter", "bluesky", "instagram"})
        try:
            lookups = await asyncio.gather(*[lookup_all(n, enabled_platforms) for n in names])
        except SerperCreditsError:
            await query.edit_message_text("Serper API credits are exhausted — handle lookups unavailable.")
            return ConversationHandler.END
        context.user_data["lookups"] = list(lookups)
        context.user_data["current_name_idx"] = 0
        return await show_next_name(update, context)

    if data == "man:remove":
        rows = [
            [InlineKeyboardButton(f"🗑 {name}", callback_data=f"man:remove_name:{i}")]
            for i, name in enumerate(names)
        ]
        rows.append([InlineKeyboardButton("← Back", callback_data="man:remove_back")])
        await query.edit_message_text("Tap a name to remove:", reply_markup=InlineKeyboardMarkup(rows))
        return AWAIT_MANUAL_NAMES

    if data.startswith("man:remove_name:"):
        i = int(data.split(":")[-1])
        names.pop(i)
        context.user_data["manual_names"] = names
        await _show_manual_names_loop(query.edit_message_text, names)
        return AWAIT_MANUAL_NAMES

    if data == "man:remove_back":
        await _show_manual_names_loop(query.edit_message_text, names)
        return AWAIT_MANUAL_NAMES


# ── Name selection ────────────────────────────────────────────────────────────

def _build_select_keyboard(names: list[str], selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, name in enumerate(names):
        mark = "✅" if i in selected else "❌"
        rows.append([InlineKeyboardButton(f"{mark} {name}", callback_data=f"sel:toggle:{i}")])
    action_row = [InlineKeyboardButton("Skip all", callback_data="sel:skip")]
    if selected:
        action_row.append(InlineKeyboardButton("🔍 Search socials", callback_data="sel:confirm"))
    rows.append(action_row)
    return InlineKeyboardMarkup(rows)


async def handle_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    names = context.user_data["detected_names"]
    selected: set = context.user_data["selected_name_indices"]

    if data.startswith("sel:toggle:"):
        idx = int(data.split(":")[-1])
        if idx in selected:
            selected.discard(idx)
        else:
            selected.add(idx)
        await query.edit_message_reply_markup(reply_markup=_build_select_keyboard(names, selected))
        return SELECT_NAMES

    if data == "sel:skip":
        await query.edit_message_text("Formatting…")
        await send_formatted_output(update, context)
        return ConversationHandler.END

    # sel:confirm
    chosen = [names[i] for i in sorted(selected)]
    await query.edit_message_text(f"Looking up {len(chosen)} name(s)…")
    enabled_platforms = context.user_data.get("enabled_platforms", {"twitter", "bluesky", "instagram"})
    try:
        lookups = await asyncio.gather(*[lookup_all(n, enabled_platforms) for n in chosen])
    except SerperCreditsError:
        await query.edit_message_text("Serper API credits are exhausted — handle lookups unavailable.")
        return ConversationHandler.END
    context.user_data["lookups"] = list(lookups)
    context.user_data["current_name_idx"] = 0
    return await show_next_name(update, context)


# ── Main text receiver ─────────────────────────────────────────────────────────

@authorized_only
async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # allow_reentry=True causes PTB to check entry points before state handlers,
    # so a search query typed in AWAIT_HANDLE_INPUT ends up here. Delegate back.
    if context.user_data.get("editing_handle"):
        return await receive_handle_input(update, context)

    message = update.message
    text = (message.text or message.caption or "").strip()

    if not text:
        await message.reply_text("Send me some text to format.")
        return ConversationHandler.END

    old_msg_id = context.user_data.get("name_message_id")
    if old_msg_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=update.effective_chat.id,
                message_id=old_msg_id,
                reply_markup=None,
            )
        except Exception as e:
            logger.warning("Failed to clear old name keyboard: %s", e)

    config = load_config()
    enabled_platforms = {p for p in ("twitter", "bluesky", "instagram") if config.get(p, {}).get("enabled", True)}

    context.user_data.update({
        "original_text": text,
        "substitutions": {},
        "current_name_idx": 0,
        "lookups": [],
        "resolved": {},
        "enabled_platforms": enabled_platforms,
    })

    names = extract_names(text, config.get("ignored_names", []))

    if not names:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, add names", callback_data="detect:yes"),
            InlineKeyboardButton("No, just format", callback_data="detect:no"),
        ]])
        await message.reply_text(
            "No names detected. Want to add names manually?",
            reply_markup=keyboard,
        )
        return CONFIRM_NAMES

    selected = set(range(len(names)))
    context.user_data["detected_names"] = names
    context.user_data["selected_name_indices"] = selected
    await message.reply_text(
        f"Names detected. Select which to look up socials for:",
        reply_markup=_build_select_keyboard(names, selected),
    )
    return SELECT_NAMES


# ── Setup wizard (/start and /config) ─────────────────────────────────────────

PLATFORM_SETUP = [
    ("twitter",   "🐦 Twitter"),
    ("bluesky",   "🦋 Bluesky"),
    ("instagram", "📸 Instagram"),
]


def _build_platforms_keyboard(enabled_map: dict, auto_paragraph: bool, auto_emoji: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for platform, label in PLATFORM_SETUP:
        mark = "✅" if enabled_map[platform] else "❌"
        rows.append([InlineKeyboardButton(
            f"{mark} {label}",
            callback_data=f"setup:toggle:{platform}",
        )])
    ap_mark = "✅" if auto_paragraph else "❌"
    rows.append([InlineKeyboardButton(f"{ap_mark} Auto-paragraph", callback_data="setup:toggle_ap")])
    if auto_paragraph:
        ae_mark = "✅" if auto_emoji else "❌"
        rows.append([InlineKeyboardButton(f"{ae_mark} Auto-emoji", callback_data="setup:toggle_ae")])
    any_enabled = any(enabled_map.values())
    if any_enabled:
        rows.append([InlineKeyboardButton("Next →", callback_data="setup:next")])
    return InlineKeyboardMarkup(rows)


def _build_steps_list(enabled_map: dict) -> list:
    steps = []
    for platform, _ in PLATFORM_SETUP:
        if enabled_map[platform]:
            steps.append((platform, "prefix"))
            steps.append((platform, "suffix"))
    steps.append(("ignored_names", "ignored_names"))
    return steps


def _seed_setup_data(context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    context.user_data["setup_platform_enabled"] = {
        p: cfg.get(p, {}).get("enabled", True)
        for p, _ in PLATFORM_SETUP
    }
    context.user_data["setup_auto_paragraph"] = cfg.get("auto_paragraph", False)
    context.user_data["setup_auto_emoji"] = cfg.get("auto_emoji", False)
    context.user_data["setup_steps"] = []
    context.user_data["setup_index"] = 0
    context.user_data["setup_pending"] = {}


async def _show_platforms_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    enabled_map = context.user_data["setup_platform_enabled"]
    auto_paragraph = context.user_data.get("setup_auto_paragraph", False)
    auto_emoji = context.user_data.get("setup_auto_emoji", False)
    keyboard = _build_platforms_keyboard(enabled_map, auto_paragraph, auto_emoji)
    await update.message.reply_text(
        "Which platforms are you posting to? Tap to toggle, then press Next.",
        reply_markup=keyboard,
    )
    return SETUP_PLATFORMS


@authorized_only
async def setup_start_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _seed_setup_data(context)
    await update.message.reply_text(
        "Welcome! Let's set up your formatter.\n\n"
        "You can run /start again at any time to update these settings."
    )
    return await _show_platforms_step(update, context)


@authorized_only
async def setup_config_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _seed_setup_data(context)
    await update.message.reply_text("Updating your config.")
    return await _show_platforms_step(update, context)


async def setup_platforms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "setup:toggle_ap":
        new_ap = not context.user_data.get("setup_auto_paragraph", False)
        context.user_data["setup_auto_paragraph"] = new_ap
        if not new_ap:
            context.user_data["setup_auto_emoji"] = False
        await query.edit_message_reply_markup(
            reply_markup=_build_platforms_keyboard(
                context.user_data["setup_platform_enabled"],
                context.user_data["setup_auto_paragraph"],
                context.user_data.get("setup_auto_emoji", False),
            )
        )
        return SETUP_PLATFORMS

    if data == "setup:toggle_ae":
        context.user_data["setup_auto_emoji"] = not context.user_data.get("setup_auto_emoji", False)
        await query.edit_message_reply_markup(
            reply_markup=_build_platforms_keyboard(
                context.user_data["setup_platform_enabled"],
                context.user_data.get("setup_auto_paragraph", False),
                context.user_data["setup_auto_emoji"],
            )
        )
        return SETUP_PLATFORMS

    if data.startswith("setup:toggle:"):
        platform = data.split(":", 2)[2]
        enabled_map = context.user_data["setup_platform_enabled"]
        enabled_map[platform] = not enabled_map[platform]
        await query.edit_message_reply_markup(
            reply_markup=_build_platforms_keyboard(
                enabled_map,
                context.user_data.get("setup_auto_paragraph", False),
                context.user_data.get("setup_auto_emoji", False),
            )
        )
        return SETUP_PLATFORMS

    if data == "setup:next":
        enabled_map = context.user_data["setup_platform_enabled"]
        context.user_data["setup_steps"] = _build_steps_list(enabled_map)
        context.user_data["setup_index"] = 0
        context.user_data["setup_pending"] = {}
        return await _show_field_step(update, context)


def _field_label(platform: str, field: str) -> str:
    if platform == "ignored_names":
        return "Ignored names (comma-separated)"
    labels = {"twitter": "🐦 Twitter", "bluesky": "🦋 Bluesky", "instagram": "📸 Instagram"}
    return f"{labels[platform]} {field}"


def _current_field_value(platform: str, field: str) -> str:
    cfg = load_config()
    if platform == "ignored_names":
        return ", ".join(cfg.get("ignored_names", []))
    return cfg.get(platform, {}).get(field, "")


async def _show_field_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    steps = context.user_data["setup_steps"]
    idx = context.user_data["setup_index"]

    if idx >= len(steps):
        return await _show_confirm_step(update, context)

    platform, field = steps[idx]
    label = _field_label(platform, field)
    current = _current_field_value(platform, field)
    display = current if current else "(empty)"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Keep", callback_data="setup:keep"),
    ]])
    text = f"<b>{_esc(label)}</b>\nCurrent: <code>{_esc(display)}</code>\n\nType a new value, or press Keep."

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    return SETUP_FIELD


def _names_loop_keyboard(accumulated: list) -> InlineKeyboardMarkup:
    rows = []
    if accumulated:
        rows.append([InlineKeyboardButton("🗑 Remove a name", callback_data="setup:remove_names")])
    rows.append([InlineKeyboardButton("✓ Done", callback_data="setup:done_names")])
    return InlineKeyboardMarkup(rows)


async def _show_names_loop(send_fn, accumulated: list):
    display = ",".join(accumulated) if accumulated else "(none)"
    await send_fn(
        f"Names so far: <code>{_esc(display)}</code>\n\nType another name to add, or press Done.",
        parse_mode="HTML",
        reply_markup=_names_loop_keyboard(accumulated),
    )


async def setup_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    steps = context.user_data["setup_steps"]
    idx = context.user_data["setup_index"]
    platform, field = steps[idx]

    if query.data == "setup:done_names":
        context.user_data["setup_index"] = idx + 1
        return await _show_field_step(update, context)

    if query.data == "setup:remove_names":
        accumulated = context.user_data["setup_pending"].get(("ignored_names", "ignored_names"), [])
        rows = [
            [InlineKeyboardButton(f"🗑 {name}", callback_data=f"setup:remove_name:{i}")]
            for i, name in enumerate(accumulated)
        ]
        rows.append([InlineKeyboardButton("← Back", callback_data="setup:remove_back")])
        await query.edit_message_text(
            "Tap a name to remove it:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return SETUP_FIELD

    if query.data.startswith("setup:remove_name:"):
        i = int(query.data.split(":")[-1])
        accumulated = context.user_data["setup_pending"].get(("ignored_names", "ignored_names"), [])
        accumulated.pop(i)
        context.user_data["setup_pending"][("ignored_names", "ignored_names")] = accumulated
        await _show_names_loop(query.edit_message_text, accumulated)
        return SETUP_FIELD

    if query.data == "setup:remove_back":
        accumulated = context.user_data["setup_pending"].get(("ignored_names", "ignored_names"), [])
        await _show_names_loop(query.edit_message_text, accumulated)
        return SETUP_FIELD

    # Keep current value
    current = _current_field_value(platform, field)
    if platform == "ignored_names":
        context.user_data["setup_pending"][("ignored_names", "ignored_names")] = [
            n.strip() for n in current.split(",") if n.strip()
        ]
    else:
        context.user_data["setup_pending"][(platform, field)] = current

    context.user_data["setup_index"] = idx + 1
    return await _show_field_step(update, context)


async def setup_field_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    steps = context.user_data["setup_steps"]
    idx = context.user_data["setup_index"]
    platform, field = steps[idx]
    value = update.message.text.strip()

    if platform == "ignored_names":
        new_names = [n.strip() for n in value.split(",") if n.strip()]
        key = ("ignored_names", "ignored_names")
        if key not in context.user_data["setup_pending"]:
            current_cfg = _current_field_value("ignored_names", "ignored_names")
            context.user_data["setup_pending"][key] = [n.strip() for n in current_cfg.split(",") if n.strip()]
        existing = context.user_data["setup_pending"][key]
        accumulated = existing + new_names
        context.user_data["setup_pending"][key] = accumulated
        await _show_names_loop(update.message.reply_text, accumulated)
        return SETUP_FIELD
    else:
        context.user_data["setup_pending"][(platform, field)] = value
        context.user_data["setup_index"] = idx + 1
        return await _show_field_step(update, context)


def _build_confirm_message(enabled_map: dict, pending: dict, auto_paragraph: bool, auto_emoji: bool = False) -> str:
    lines = ["<b>Ready to save:</b>\n"]
    for platform, label in PLATFORM_SETUP:
        enabled = enabled_map[platform]
        mark = "✅" if enabled else "❌"
        lines.append(f"{label}: {mark}")
        if enabled:
            prefix = pending.get((platform, "prefix"), _current_field_value(platform, "prefix"))
            suffix = pending.get((platform, "suffix"), _current_field_value(platform, "suffix"))
            lines.append(f"  Prefix: <code>{_esc(prefix or '(empty)')}</code>")
            lines.append(f"  Suffix: <code>{_esc(suffix or '(empty)')}</code>")
    ignored = pending.get(
        ("ignored_names", "ignored_names"),
        load_config().get("ignored_names", [])
    )
    lines.append(f"\n🚫 Ignored names: <code>{_esc(', '.join(ignored) or '(none)')}</code>")
    ap_mark = "✅" if auto_paragraph else "❌"
    lines.append(f"📐 Auto-paragraph: {ap_mark}")
    if auto_paragraph:
        ae_mark = "✅" if auto_emoji else "❌"
        lines.append(f"😀 Auto-emoji: {ae_mark}")
    return "\n".join(lines)


async def _show_confirm_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    enabled_map = context.user_data["setup_platform_enabled"]
    pending = context.user_data["setup_pending"]
    auto_paragraph = context.user_data.get("setup_auto_paragraph", False)
    auto_emoji = context.user_data.get("setup_auto_emoji", False)
    text = _build_confirm_message(enabled_map, pending, auto_paragraph, auto_emoji)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💾 Save", callback_data="setup:save"),
    ]])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    return SETUP_CONFIRM


async def setup_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cfg = load_config()
    enabled_map = context.user_data["setup_platform_enabled"]
    pending = context.user_data["setup_pending"]

    cfg["auto_paragraph"] = context.user_data.get("setup_auto_paragraph", False)
    cfg["auto_emoji"] = context.user_data.get("setup_auto_emoji", False)

    for platform, _ in PLATFORM_SETUP:
        cfg.setdefault(platform, {})["enabled"] = enabled_map[platform]

    for (platform, field), value in pending.items():
        if platform == "ignored_names":
            cfg["ignored_names"] = value
        else:
            cfg.setdefault(platform, {})[field] = value

    save_config(cfg)
    await query.edit_message_text("✅ Config saved! Send me any text to format.")
    return ConversationHandler.END


# ── /users command (admin only) ───────────────────────────────────────────────

def build_users_message(allowed: list[int]) -> str:
    if not allowed:
        return "👥 <b>Allowed users</b>\n\n<i>(none)</i>"
    lines = ["👥 <b>Allowed users</b>\n"]
    for uid in allowed:
        lines.append(f"• <code>{uid}</code>")
    return "\n".join(lines)


def build_users_keyboard(allowed: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ Add user", callback_data="usr:add")]]
    if allowed:
        rows.append([InlineKeyboardButton("🗑 Delete user", callback_data="usr:delete")])
    return InlineKeyboardMarkup(rows)


def build_delete_keyboard(allowed: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(str(uid), callback_data=f"usr:remove:{uid}")] for uid in allowed]
    return InlineKeyboardMarkup(rows)


@admin_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    allowed = cfg.get("allowed_users", [])
    await update.message.reply_text(
        build_users_message(allowed),
        parse_mode="HTML",
        reply_markup=build_users_keyboard(allowed),
    )
    return MANAGE_USERS


async def handle_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cfg = load_config()
    allowed = cfg.get("allowed_users", [])

    if query.data == "usr:add":
        await query.edit_message_text("Send the Telegram user ID to add:")
        return ADD_USER

    if query.data == "usr:delete":
        await query.edit_message_text(
            "Select a user to remove:",
            reply_markup=build_delete_keyboard(allowed),
        )
        return DELETE_USER

    if query.data.startswith("usr:remove:"):
        uid = int(query.data.split(":")[-1])
        allowed = [u for u in allowed if u != uid]
        cfg["allowed_users"] = allowed
        save_config(cfg)

    await query.edit_message_text(
        build_users_message(allowed),
        parse_mode="HTML",
        reply_markup=build_users_keyboard(allowed),
    )
    return MANAGE_USERS


async def receive_new_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        uid = int(raw)
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid user ID. Try again or /cancel.")
        return ADD_USER
    cfg = load_config()
    allowed = cfg.get("allowed_users", [])
    if uid not in allowed:
        allowed.append(uid)
        cfg["allowed_users"] = allowed
        save_config(cfg)

    await update.message.reply_text(
        build_users_message(allowed),
        parse_mode="HTML",
        reply_markup=build_users_keyboard(allowed),
    )
    return MANAGE_USERS


# ── Error handler ─────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.warning("Update caused error: %s", context.error)


# ── /cancel ────────────────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("editing_handle", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    # Pre-load spaCy model so the first message isn't slow
    from names import get_nlp
    get_nlp()

    setup_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", setup_start_entry),
            CommandHandler("config", setup_config_entry),
        ],
        states={
            SETUP_PLATFORMS: [
                CallbackQueryHandler(setup_platforms_callback, pattern="^setup:"),
            ],
            SETUP_FIELD: [
                CallbackQueryHandler(setup_field_callback, pattern="^setup:(keep|done_names|remove_names|remove_name:\\d+|remove_back)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setup_field_input),
            ],
            SETUP_CONFIRM: [
                CallbackQueryHandler(setup_confirm_callback, pattern="^setup:save$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_message=False,
    )

    users_conv = ConversationHandler(
        entry_points=[CommandHandler("users", users_command)],
        states={
            MANAGE_USERS: [CallbackQueryHandler(handle_users_callback, pattern="^usr:")],
            ADD_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_user_id)],
            DELETE_USER: [CallbackQueryHandler(handle_users_callback, pattern="^usr:remove:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
        ],
        states={
            SELECT_NAMES: [
                CallbackQueryHandler(handle_select_callback, pattern="^sel:"),
            ],
            CONFIRM_NAMES: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: u.message.reply_text("Please use the buttons above to continue.")),
            ],
            AWAIT_MANUAL_NAMES: [
                CallbackQueryHandler(handle_manual_names_callback, pattern="^man:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_manual_names),
            ],
            AWAIT_HANDLE_INPUT: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_handle_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(setup_conv)
    app.add_handler(users_conv)
    app.add_handler(conv)
    app.add_error_handler(error_handler)
    app.run_polling()


if __name__ == "__main__":
    main()
