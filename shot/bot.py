import asyncio
import dataclasses
import datetime
from pathlib import Path

from aiotg import Bot, Chat
from loguru import logger

from shot import conf
from shot.conf.model import Cam
from shot.keyboards import CamerasChannel, Menu
from shot.model import Admin, Channel, db
from shot.model.helpers import ThreadSwitcherWithDB, db_in_thread
from shot.shooter import get_img, make_movie


async def unhandled_callbacks(chat, cq):
    await cq.answer()
    await chat.send_text('Unhandled callback!')


async def get_cam(name, chat):
    if name not in conf.cameras:
        await chat.send_text('Wrong cam name!')
        return
    return conf.cameras[name]


async def mov(chat, match):
    cam = await get_cam(match.group(2), chat)
    if not cam:
        return
    day = match.group(1)
    loop = asyncio.get_event_loop()
    clip = await loop.run_in_executor(None, make_movie, cam, day)
    with open(clip, 'rb') as clip:
        await chat.send_video(clip)


async def regular_handler(chat, cam_name):
    cam = await get_cam(cam_name, chat)
    if not cam:
        return
    day = datetime.datetime.now() - datetime.timedelta(days=1)
    day = day.strftime('%d_%m_%Y')
    clip = Path(conf.root_dir) / 'data' / cam.name / 'regular' / 'clips' / f'{day}.mp4'
    if not clip.exists():
        await chat.send_text(f'Can not find regular clip for {day}!')
        return
    with open(clip, 'rb') as clip:
        await chat.send_video(clip)


async def regular(chat, cq, match):
    await cq.answer()
    await regular_handler(chat, match.group(1))


async def today(chat, cq, match):
    await cq.answer(text='Going to make movie till now..')
    await today_handler(chat, match.group(1))


async def today_handler(chat, cam_name):
    cam = await get_cam(cam_name, chat)
    if not cam:
        return
    today = datetime.datetime.now().strftime('%d_%m_%Y')
    loop = asyncio.get_event_loop()
    clip = await loop.run_in_executor(None, lambda: make_movie(cam, today, regular=False))
    with open(clip, 'rb') as clip:
        await chat.send_video(clip)


@ThreadSwitcherWithDB.optimized
async def reg(chat: Chat, match):
    async with db_in_thread():
        admin = db.query(Admin).filter(Admin.chat_id == chat.id).one_or_none()
    if admin:
        await chat.send_text('You are already registered!')
        return
    async with db_in_thread():
        admin = Admin(chat_id=chat.id)
        db.add(admin)
        db.commit()
    await chat.send_text('You are successfully registered!')


class CamBot:

    def __init__(self):
        self._bot = Bot(conf.bot_token, proxy=conf.tele_proxy)
        self.session = self._bot.session
        self.loop = self._bot.loop
        self.menu_markup = Menu()
        self.init_handlers()

    def init_handlers(self):
        self._bot.add_command(r'/mov (.+) (.+)', mov)
        self._bot.add_command(r'/reg', reg)
        self._bot.add_command(r'/ch', self.reg_channel)
        self._bot.add_command(r'/menu', self.menu)
        self._bot.add_callback(r'regular (.+)', regular)
        self._bot.add_callback(r'today (.+)', today)
        self._bot.add_callback(r'select (.+)', self.select)
        self._bot.add_callback(r'back', self.back)
        self._bot.add_callback(r'img (.+)', self.img_callback)
        self._bot.add_callback(r'choose_cam (.+)', self.choose_cam_callback)
        self._bot.callback(unhandled_callbacks)

    @ThreadSwitcherWithDB.optimized
    async def daily_movie(self, cam: Cam):
        day = datetime.datetime.now() - datetime.timedelta(days=1)
        day = day.strftime('%d_%m_%Y')
        loop = asyncio.get_event_loop()
        try:
            path = await loop.run_in_executor(None, make_movie, cam, day)
        except FileNotFoundError as exc:
            logger.exception(exc)
            await self.notify_admins(f'File {exc.filename} not found for daily movie {cam.name}: {day}')
            return
        except Exception as exc:
            logger.exception(exc)
            await self.notify_admins(f'Error during making daily movie for {cam.name}: {day}')
            return
        if cam.update_channel:
            async with db_in_thread():
                channels = db.query(Channel).filter(Channel.cam == cam.name).all()
            for channel in channels:
                clip = open(path, 'rb')
                await Chat(self._bot, channel.chat_id).send_video(clip)
                # TODO find out why file here is closed
                clip.close()

    async def img_handler(self, chat: Chat, match):
        cam = await get_cam(match.group(1), chat)
        if not cam:
            return
        image = await get_img(cam, self._bot.session, regular=False)
        if not image:
            await chat.send_text('Error during image request :(')
        with open(image, 'rb') as image:
            await chat.send_photo(image)

    async def img_callback(self, chat, cq, match):
        await cq.answer()
        await self.img_handler(chat, match)

    @ThreadSwitcherWithDB.optimized
    async def reg_channel(self, chat: Chat, match):
        async with db_in_thread():
            channel = db.query(Channel).filter(Channel.chat_id == chat.id).one_or_none()
        if channel:
            await self.notify_admins(f'Channel {chat.id} already registered!')
            return
        await chat.send_text('Choose cam for channel', reply_markup=CamerasChannel().options.to_json())

    @ThreadSwitcherWithDB.optimized
    async def choose_cam_callback(self, chat, cq, match):
        cam = match.group(1)
        async with db_in_thread():
            channel = Channel(chat_id=chat.id, cam=cam)
            db.add(channel)
            db.commit()
        await cq.answer(text=f'Added channel for {cam}')
        await self.notify_admins(text=f'Added channel {chat.id} for {cam}')

    @ThreadSwitcherWithDB.optimized
    async def notify_admins(self, text):
        async with db_in_thread():
            admins = db.query(Admin).all()
        for admin in admins:
            await self._bot.send_message(admin.chat_id, text)

    async def menu(self, chat, match):
        await chat.send_text('Menu', reply_markup=self.menu_markup.main_menu.to_json())

    async def select(self, chat: Chat, cq, match):
        await cq.answer()
        cam = match.group(1)
        await chat.edit_text(
            cq.src['message']['message_id'], f'Camera: {cam}',
            markup=dataclasses.asdict(self.menu_markup.cam_options[cam].markup)
        )

    async def back(self, chat, cq, match):
        await cq.answer()
        await chat.edit_text(
            cq.src['message']['message_id'], 'Menu',
            markup=dataclasses.asdict(self.menu_markup.main_menu)
        )