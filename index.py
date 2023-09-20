import asyncio
import atexit
import datetime
import functools
import hashlib
import os
import re
from multiprocessing import Process

import icalendar
import redis
import requests
from aiogram import Bot, Dispatcher, executor, types
from flask import Flask

if not os.getenv("TOKEN"):
    raise RuntimeError("TOKEN environment variable is not set")

if not os.getenv("SALT"):
    raise RuntimeError("SALT environment variable is not set")

bot = Bot(token=os.getenv("TOKEN"), parse_mode="HTML")
dp = Dispatcher(bot)

db = redis.from_url("redis://:FTePkN2fK8W4Syem3KBfFFhf7wBemGE7@redis:6379/0")


def user_id_to_token(user_id: int) -> str:
    """
    Converts Telegram user id to token
    This is needed to prevent the enumeration of information about users
    :param user_id: Telegram user id
    :return: token
    """
    return hashlib.sha256(f"{user_id}{os.getenv('SALT')}".encode()).hexdigest()


def run_sync(func, *args, **kwargs):
    """
    Run a non-async function in a new thread and return an awaitable
    :param func: Sync-only function to execute
    :return: Awaitable coroutine
    """
    return asyncio.get_event_loop().run_in_executor(
        None,
        functools.partial(func, *args, **kwargs),
    )


@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    if db.get(user_id_to_token(message.from_user.id)):
        await message.answer(
            "🧘‍♀️ <b>Hello and welcome!</b>\n\nThis is your iCal url to add to Google"
            " Calendar:"
            f" https://ical.dan.tatar/iu/sport/{user_id_to_token(message.from_user.id)}\n\nIf"
            " you have troubles with calendar, send your token from"
            " https://sport.innopolis.university again."
        )
    else:
        await message.answer(
            "🧘‍♀️ <b>Hello and welcome!</b>\n\nYou need to complete authorization"
            " process to use this bot:\n\n1. Go to the <a"
            " href='https://sport.innopolis.university'>sport website</a>\n2. Open"
            " DevTools using <b>Ctrl+Shift+I</b>\n3. Go to tab <b>Application</b>\n4."
            " Open dropdown menu <b>Cookies</b> and select"
            " <b>https://sport.innopolis.university</b>\n5. Copy the value of cookie"
            " <b>sessionid</b> and send it here"
        )


@dp.message_handler(content_types=["text"])
async def update_token(message: types.Message):
    status = await message.answer("️🧑‍💻 <b>Checking your credentials...</b>")

    answer = await run_sync(
        requests.get,
        "https://sport.innopolis.university/profile",
        cookies={"sessionid": message.text},
    )

    await status.delete()

    if answer.status_code != 200 or not answer.url.startswith("https://sport.innopolis.university"):
        await message.answer("🧘‍♀️ <b>Authorization failed!</b>\n\nPlease try again")
        return

    if name := re.search(r"<h1 class=\"card-title\">(.*?)</h1>", answer.text):
        name = name.group(1)
    else:
        name = "Student"

    name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    db.set(user_id_to_token(message.from_user.id), message.text)
    await message.answer_animation(
        "https://i.pinimg.com/originals/a5/9e/47/a59e4748afd790aa94569e35f4b2e962.gif",
        caption=(
            f"🧘‍♀️ <b>Welcome, {name}!</b>\n\nThis is your iCal url to add to Google"
            " Calendar:"
            f" https://ical.dan.tatar/iu/sport/{user_id_to_token(message.from_user.id)}\n\n<i>You"
            " can forget about the existence of this bot.</i>"
        ),
    )


def ical_from_token(token: str):
    answer = requests.get(
        "https://sport.innopolis.university/api/calendar/trainings",
        cookies={"sessionid": token},
        params={
            "start": (
                datetime.datetime.now()
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .strftime("%Y-%m-%dT%H:%M:%S")
            ),
            "end": (
                (datetime.datetime.now() + datetime.timedelta(days=14))
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .strftime("%Y-%m-%dT%H:%M:%S")
            ),
            "timeZone": "Europe/Moscow",
        },
    )

    main_calendar = icalendar.Calendar(
        prodid="Sport Schedule",
        version="2.0",
        method="PUBLISH",
    )
    main_calendar["x-wr-calname"] = "Sport in Innopolis"
    main_calendar["x-wr-timezone"] = "Europe/Moscow"
    main_calendar["x-wr-caldesc"] = (
        "Your trainings in Sport Complex by Innopolis University"
    )

    for event in answer.json():
        if not event["extendedProps"]["checked_in"]:
            continue

        ical_event = icalendar.Event()
        ical_event.add("summary", event["title"])
        ical_event.add("dtstart", datetime.datetime.fromisoformat(event["start"]))
        ical_event.add("dtend", datetime.datetime.fromisoformat(event["end"]))
        ical_event.add(
            "dtstamp",
            datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
        )
        ical_event.add("uid", event["extendedProps"]["id"])
        ical_event.add("location", event["extendedProps"]["training_class"])
        if not (details := db.get(f"event_cache_{event['extendedProps']['id']}")):
            details = requests.get(
                f"https://sport.innopolis.university/api/training/{event['extendedProps']['id']}",
                cookies={"sessionid": token},
            )
            details = details.json()["training"]
            details = (
                details["group"]["sport"]["description"]
                + "\n\nTeacher(-s): "
                + ", ".join(
                    map(
                        lambda x: f"{x['first_name']} {x['last_name']} {x['email']}",
                        details["group"]["teachers"],
                    )
                )
                + "\nAccredited: "
                + ("Yes" if details["group"]["accredited"] else "No")
            )

            db.set(
                f"event_cache_{event['extendedProps']['id']}",
                details,
                ex=3600,
            )

        ical_event.add(
            "description",
            details,
        )

        main_calendar.add_component(ical_event)

    main_calendar.add("X-WR-TOTAL-VEVENTS", len(main_calendar.subcomponents))

    return main_calendar.to_ical()


app = Flask(__name__)


@app.route("/iu/sport/<client_id>")
def ical(client_id: str):
    if not (token := db.get(client_id)):
        return "You are not authorized", 403

    return app.response_class(
        ical_from_token(token.decode()),
        mimetype="text/calendar",
        headers={"Content-Disposition": "attachment; filename=sport_in_iu.ics"},
    )


if __name__ == "__main__":
    server = Process(
        target=functools.partial(
            app.run,
            host="0.0.0.0",
            port=9234,
            debug=False,
            use_reloader=False,
        )
    )
    server.start()
    atexit.register(server.terminate)
    executor.start_polling(dp, skip_updates=True)
