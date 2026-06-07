import os
import logging
import re
import tempfile
import asyncio
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROUP_ID  = os.environ.get("GROUP_ID")

TIKTOK_PROFILE_PATTERN = re.compile(
    r"https?://(www\.)?tiktok\.com/@[\w._-]+/?$"
)

MAX_FILE_SIZE = 49 * 1024 * 1024  # 49 Mo

# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Salut ! Je suis ton bot TikTok Downloader.\n\n"
        "Envoie-moi le lien d'un profil TikTok public, par exemple :\n"
        "https://www.tiktok.com/@nomducompte\n\n"
        "Je créerai automatiquement un topic dans ton groupe privé\n"
        "et j'y enverrai toutes les vidéos et photos du compte ! 🎬📸"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Comment utiliser ce bot :\n\n"
        "1. Copie le lien d'un profil TikTok public\n"
        "   ex : https://www.tiktok.com/@nomducompte\n"
        "2. Colle-le ici et envoie\n"
        "3. Le bot crée un topic dans ton groupe pour ce compte\n"
        "4. Toutes les vidéos et photos y sont envoyées automatiquement\n\n"
        "⚠️ Notes :\n"
        "- Comptes publics uniquement\n"
        "- Fichiers > 50 Mo ignorés automatiquement\n"
        "- Les posts photos arrivent image par image avec numérotation"
    )

# ---------------------------------------------------------------------------
# Helpers yt-dlp
# ---------------------------------------------------------------------------

def _extract_entries(url: str) -> list:
    """Récupère les métadonnées de tous les posts d'un profil TikTok."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "writeinfojson": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        return []
    if "entries" in info:
        return [e for e in info["entries"] if e is not None]
    return [info]


def _extract_photo_urls_from_entry(entry: dict) -> list:
    """
    Extrait les URLs des images d'un post slideshow TikTok.
    Cherche dans tous les champs possibles que yt-dlp peut retourner.
    Retourne une liste vide si aucune image trouvée (= c'est une vidéo).
    """
    found_urls = []

    # Champ 1 : "images" — liste de dicts avec clé "url"
    raw_images = entry.get("images")
    if raw_images and isinstance(raw_images, list):
        for img in raw_images:
            if isinstance(img, dict):
                u = img.get("url") or img.get("thumbnail") or img.get("http_headers", {})
                if isinstance(u, str) and u.startswith("http"):
                    found_urls.append(u)
            elif isinstance(img, str) and img.startswith("http"):
                found_urls.append(img)
        if found_urls:
            return found_urls

    # Champ 2 : "formats" contenant des images (ext jpg/jpeg/png/webp)
    formats = entry.get("formats") or []
    image_formats = []
    for f in formats:
        if not isinstance(f, dict):
            continue
        ext = str(f.get("ext", "")).lower()
        vcodec = str(f.get("vcodec", "none")).lower()
        acodec = str(f.get("acodec", "none")).lower()
        url = str(f.get("url", ""))
        if not url.startswith("http"):
            continue
        # C'est une image si : extension image OU aucun codec vidéo/audio
        if ext in ("jpg", "jpeg", "png", "webp") or (vcodec == "none" and acodec == "none"):
            image_formats.append((f.get("format_id", ""), url))
    if image_formats:
        return [u for _, u in image_formats]

    # Champ 3 : "thumbnails" — utilisé par certaines versions de yt-dlp pour les slideshows
    # On ne l'utilise QUE si le post n'a pas de vrai codec vidéo
    all_formats = entry.get("formats") or []
    has_video = any(
        isinstance(f, dict) and str(f.get("vcodec", "none")).lower() not in ("none", "")
        for f in all_formats
    )
    if not has_video:
        thumbnails = entry.get("thumbnails") or []
        thumb_urls = [
            t["url"] for t in thumbnails
            if isinstance(t, dict) and str(t.get("url", "")).startswith("http")
        ]
        if len(thumb_urls) > 1:
            # Plus d'un thumbnail = slideshow probable
            return thumb_urls

    return []


def _download_image_to_bytes(url: str) -> bytes | None:
    """Télécharge une image depuis une URL et retourne les bytes."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.tiktok.com/",
        }
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200 and len(resp.content) > 1000:
            return resp.content
    except Exception as e:
        logger.warning(f"Erreur téléchargement image {url} : {e}")
    return None


def _download_video(entry: dict, tmpdir: str) -> str | None:
    """Télécharge la vidéo d'un post et retourne le chemin du fichier."""
    post_url = entry.get("webpage_url") or entry.get("url")
    if not post_url:
        return None

    safe_id = re.sub(r"[^\w\-]", "_", str(entry.get("id", "video")))
    opts = {
        "outtmpl": os.path.join(tmpdir, f"{safe_id}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "writeinfojson": False,
        "skip_download": False,
        "format": "best[ext=mp4]/best",
        "extract_flat": False,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([post_url])

    for f in Path(tmpdir).iterdir():
        if f.stem == safe_id and f.suffix.lower() not in (".json", ".part", ".ytdl"):
            return str(f)
    return None

# ---------------------------------------------------------------------------
# Gestion des topics Telegram
# ---------------------------------------------------------------------------

async def _create_topic(context: ContextTypes.DEFAULT_TYPE, group_id: int, username: str) -> int | None:
    """Crée un topic dans le groupe et retourne son message_thread_id."""
    try:
        forum_topic = await context.bot.create_forum_topic(
            chat_id=group_id,
            name=f"@{username}"
        )
        return forum_topic.message_thread_id
    except Exception as e:
        logger.error(f"Impossible de créer le topic @{username} : {e}")
        return None

# ---------------------------------------------------------------------------
# Handler principal
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not TIKTOK_PROFILE_PATTERN.match(text):
        await update.message.reply_text(
            "❌ Ce lien ne semble pas être un profil TikTok valide.\n"
            "Format attendu : https://www.tiktok.com/@nomducompte"
        )
        return

    if not GROUP_ID:
        await update.message.reply_text(
            "❌ GROUP_ID manquant !\n"
            "Ajoute la variable GROUP_ID dans Railway."
        )
        return

    try:
        group_id = int(GROUP_ID)
    except ValueError:
        await update.message.reply_text(
            "❌ GROUP_ID invalide ! Il doit ressembler à -1001234567890."
        )
        return

    username = text.rstrip("/").split("/@")[-1]

    await update.message.reply_text(
        f"⏳ Récupération des posts de @{username}...\nPatiente quelques instants 🙏"
    )

    loop = asyncio.get_running_loop()

    try:
        entries = await loop.run_in_executor(None, lambda: _extract_entries(text))
    except Exception as e:
        logger.error(f"Erreur extraction @{username} : {e}")
        await update.message.reply_text(
            "❌ Impossible de récupérer ce profil.\n"
            "Vérifie que le compte est public et que le lien est correct."
        )
        return

    if not entries:
        await update.message.reply_text("😕 Aucun post trouvé sur ce profil.")
        return

    total = len(entries)

    thread_id = await _create_topic(context, group_id, username)
    if thread_id is None:
        await update.message.reply_text(
            "❌ Impossible de créer le topic dans le groupe.\n"
            "Vérifie que :\n"
            "• Le bot est admin du groupe\n"
            "• Les topics sont activés (groupe supergroupe)\n"
            "• Le GROUP_ID est correct"
        )
        return

    await update.message.reply_text(
        f"✅ {total} post(s) trouvé(s) !\n"
        f"Topic @{username} créé dans le groupe 📂\n"
        f"Envoi en cours... 📤"
    )

    sent = 0
    skipped = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        for idx, entry in enumerate(entries, start=1):
            caption_prefix = f"ssx {idx}/{total}"

            # ── PHOTOS : extraction des URLs depuis les métadonnées ──────────
            photo_urls = _extract_photo_urls_from_entry(entry)

            if photo_urls:
                photo_total = len(photo_urls)
                post_ok = True
                for photo_idx, img_url in enumerate(photo_urls, start=1):
                    photo_caption = f"{caption_prefix} 📸 photo {photo_idx}/{photo_total}"
                    # Télécharge l'image en bytes pour éviter les erreurs d'URL expirée
                    img_bytes = await loop.run_in_executor(
                        None, lambda u=img_url: _download_image_to_bytes(u)
                    )
                    if img_bytes is None:
                        logger.warning(f"Image {photo_idx} inaccessible (post {idx}), ignorée")
                        post_ok = False
                        continue
                    try:
                        await context.bot.send_photo(
                            chat_id=group_id,
                            message_thread_id=thread_id,
                            photo=img_bytes,
                            caption=photo_caption
                        )
                        await asyncio.sleep(0.4)
                    except Exception as e:
                        logger.warning(f"Erreur envoi photo {photo_idx}/{photo_total} (post {idx}) : {e}")
                        post_ok = False
                if post_ok:
                    sent += 1
                else:
                    skipped += 1
                continue

            # ── VIDÉO normale ────────────────────────────────────────────────
            try:
                filepath = await loop.run_in_executor(
                    None, lambda e=entry: _download_video(e, tmpdir)
                )
            except Exception as e:
                logger.warning(f"Erreur téléchargement vidéo (post {idx}) : {e}")
                skipped += 1
                continue

            if filepath is None:
                logger.warning(f"Fichier introuvable après téléchargement (post {idx})")
                skipped += 1
                continue

            file_size = os.path.getsize(filepath)
            if file_size > MAX_FILE_SIZE:
                try:
                    await context.bot.send_message(
                        chat_id=group_id,
                        message_thread_id=thread_id,
                        text=f"⚠️ {caption_prefix} — vidéo trop lourde "
                             f"({file_size // (1024 * 1024)} Mo), ignorée."
                    )
                except Exception:
                    pass
                skipped += 1
                os.remove(filepath)
                continue

            try:
                with open(filepath, "rb") as f:
                    await context.bot.send_video(
                        chat_id=group_id,
                        message_thread_id=thread_id,
                        video=f,
                        caption=caption_prefix,
                        supports_streaming=True
                    )
                sent += 1
            except Exception as e:
                logger.warning(f"Erreur envoi vidéo (post {idx}) : {e}")
                skipped += 1
            finally:
                if os.path.exists(filepath):
                    os.remove(filepath)

            await asyncio.sleep(1)

    # Résumé dans le topic
    summary = f"🎉 Terminé pour @{username} !\n✅ Envoyé : {sent}/{total}"
    if skipped:
        summary += f"\n⚠️ Ignoré(s) : {skipped} (trop lourd ou erreur)"
    try:
        await context.bot.send_message(
            chat_id=group_id,
            message_thread_id=thread_id,
            text=summary
        )
    except Exception as e:
        logger.warning(f"Erreur envoi résumé : {e}")

    await update.message.reply_text(
        f"✅ Terminé ! Tout est dans le topic @{username} de ton groupe."
    )

# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise ValueError(
            "❌ BOT_TOKEN manquant ! Ajoute-le dans les variables Railway."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot démarré et en écoute !")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
