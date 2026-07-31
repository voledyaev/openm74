#!/usr/bin/env python3
"""Language for the GUI: Russian and English, chosen from the system or by the user.

Two decisions shape this file.

**What gets translated.** The interface does, completely. The engine's log does not, and
that is deliberate rather than lazy: it is a stream of addresses, hex, register names, baud
rates and status bytes, and translating `ACK`, `MOV` or `baud` makes a log harder to search,
harder to compare against the docs and harder to paste into a bug report. What a user
actually needs to *understand* -- how far along the job is, whether it worked, what to do
when it did not -- does not come from that stream at all. It arrives as structured events
(see `--progress json`), and the interface renders those in whichever language is selected.
That split is why localisation here is a catalogue rather than a rewrite.

**Which language.** Russian if the system says Russian, English for everything else,
overridable by the user and remembered. The default leans English on purpose: this ECU is
mostly a Russian-speaking audience, so the Russian half of the interface is the one that
must be right -- but a tool that silently speaks Russian to someone whose machine is set to
Portuguese is a tool they cannot use at all.
"""
import os
import sys

LANGS = ("ru", "en")
DEFAULT = "en"
_lang = DEFAULT


def detect():
    """The language this machine is set to, as best it can be asked.

    Environment first, so a deliberate override always wins and so tests can pin it. Then
    the platform, because the environment is exactly what a GUI application does NOT get:
    a bundle launched from a desktop inherits almost none of a shell's variables, which is
    why LANG alone would report English to half the people this is written for.
    """
    for var in ("OPENM74_LANG", "LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var)
        if v:
            return "ru" if v.strip().lower().startswith("ru") else "en"

    if sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.check_output(
                ["defaults", "read", "-g", "AppleLocale"],
                stderr=subprocess.DEVNULL, timeout=5)
            return "ru" if out.decode("utf-8", "replace").strip().lower().startswith("ru") \
                else "en"
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes
            # low 10 bits are the primary language; 0x19 is LANG_RUSSIAN
            return "ru" if (ctypes.windll.kernel32.GetUserDefaultUILanguage()
                            & 0x3FF) == 0x19 else "en"
        except Exception:
            pass

    try:
        import locale
        loc = (locale.getdefaultlocale()[0] or "")
        return "ru" if loc.lower().startswith("ru") else "en"
    except Exception:
        return DEFAULT


def set_lang(code):
    global _lang
    _lang = code if code in LANGS else DEFAULT
    return _lang


def get_lang():
    return _lang


def t(key, *args):
    """Look a string up in the current language.

    A missing key returns the key itself rather than raising: a half-translated build should
    look obviously unfinished, not refuse to open the window someone needs to flash an ECU.
    tools/check_gui_platform.py fails on any key missing from any language, so 'obviously
    unfinished' is a thing that gets caught before shipping rather than in front of a user.
    """
    row = STRINGS.get(key)
    if row is None:
        return key
    s = row.get(_lang) or row.get(DEFAULT) or key
    return (s % args) if args else s


def missing():
    """Every (key, language) pair that has no string.  Used by the platform checks."""
    out = []
    for key, row in STRINGS.items():
        for lang in LANGS:
            if not row.get(lang):
                out.append((key, lang))
    return out


# ---------------------------------------------------------------------------
# The catalogue.  Keys are dotted and grouped by where they appear, so a screen can be
# reviewed by reading one block rather than by hunting.
STRINGS = {

    # --- identity ---------------------------------------------------------
    "app.title": {
        "ru": u"openm74 — чтение и запись флеша ЭБУ M74 CAN",
        "en": u"openm74 — read and write the M74 CAN ECU flash",
    },
    "app.device": {
        "ru": u"ЭБУ Итэлма M74 CAN · 11183-1411020-62 · плата M74_v6.36 · Infineon XC2765X",
        "en": u"Itelma M74 CAN ECU · 11183-1411020-62 · board M74_v6.36 · Infineon XC2765X",
    },

    # --- main window ------------------------------------------------------
    "ui.adapter": {"ru": u"Адаптер:", "en": u"Adapter:"},
    "ui.refresh": {"ru": u"Обновить", "en": u"Refresh"},
    "ui.image_file": {"ru": u"Файл образа:", "en": u"Image file:"},
    "ui.choose": {"ru": u"Выбрать…", "en": u"Choose…"},
    "menu.view": {"ru": u"Вид", "en": u"View"},
    "menu.help": {"ru": u"Помощь", "en": u"Help"},
    "menu.language": {"ru": u"Язык", "en": u"Language"},
    "menu.theme": {"ru": u"Оформление", "en": u"Appearance"},
    "menu.theme_auto": {"ru": u"Как в системе", "en": u"Match the system"},
    "menu.theme_light": {"ru": u"Светлое", "en": u"Light"},
    "menu.theme_dark": {"ru": u"Тёмное", "en": u"Dark"},
    "ui.mode_frame": {"ru": u"Режим записи", "en": u"Write mode"},
    "ui.mode_reliable": {
        "ru": u"Надёжно — полная побайтная сверка каждого сектора и всего образа в конце. "
              u"Дольше.",
        "en": u"Reliable — every sector verified byte for byte, and the whole image read "
              u"back at the end. Slower.",
    },
    "ui.mode_fast": {
        # "вдвое"/"twice" was never measured: 206 sectors come out at ~8 min fast against
        # ~13 reliable, which is about 1.6x.  The CLI's own --mode help says "about a third
        # quicker"; this label is the same claim and must not disagree with it.
        "ru": u"Быстро — сверка каждого сектора по контрольной сумме. Примерно на треть "
              u"быстрее.",
        "en": u"Fast — each sector verified by a checksum the ECU computes. About a third "
              u"quicker.",
    },
    "ui.backup_note": {
        "ru": u"Полный бэкап перед записью снимается в обоих режимах, и без него запись не "
              u"начнётся.",
        "en": u"A full backup is taken before writing in both modes, and a write that "
              u"cannot get one does not start.",
    },
    "ui.resume_check": {
        "ru": u"Продолжить прерванную запись — читать каждый сектор и пропускать уже "
              u"правильные",
        "en": u"Resume an interrupted write — read every sector and skip the ones already "
              u"correct",
    },
    "ui.read_btn": {"ru": u"Прочитать образ из ЭБУ", "en": u"Read image from the ECU"},
    "ui.write_btn": {"ru": u"Записать образ в ЭБУ", "en": u"Write image to the ECU"},
    "ui.help_btn": {"ru": u"Справка", "en": u"Help"},
    "ui.log": {"ru": u"Журнал:", "en": u"Log:"},
    "ui.copy": {"ru": u"Копировать", "en": u"Copy"},
    "ui.save_as": {"ru": u"Сохранить в файл…", "en": u"Save to file…"},
    "ui.clear": {"ru": u"Очистить", "en": u"Clear"},
    "menu.select_all": {"ru": u"Выделить всё", "en": u"Select all"},
    "menu.advanced": {"ru": u"Дополнительно", "en": u"Advanced"},
    "menu.force_ecu": {
        "ru": u"ОПАСНО: писать в блок, который не опознан",
        "en": u"DANGER: write to an ECU that is not recognised",
    },
    "dlg.force_title": {"ru": u"Отключить проверку блока?", "en": u"Turn the ECU check off?"},
    "dlg.force_body": {
        "ru": u"Перед записью программа спрашивает у кристалла, что он такое, и отказывается "
              u"писать, если это не тот процессор, под который собраны загружаемые в ЭБУ "
              u"куски кода.\n\n"
              u"Проверка нужна не от того, что вы выбрали не тот файл. Она нужна от другого: "
              u"в этом же семействе есть процессоры, которые отвечают на то же рукопожатие, "
              u"честно сообщают те же 832 КБ и имеют ДРУГУЮ карту памяти. Глазами это не "
              u"отличить. Код, залитый в такой блок, выполнится по чужим адресам.\n\n"
              u"Отключать её осмысленно, если вы точно знаете, что ваш блок совместим — "
              u"например по списку в документации. Всё остальное вы берёте на себя.\n\n"
              u"Отключить до закрытия программы?",
        "en": u"Before writing, the program asks the silicon what it is and refuses if it "
              u"is not the processor the code uploaded into the ECU was built for.\n\n"
              u"That check is not there because you might have picked the wrong file. It is "
              u"there because this family contains processors that answer the same "
              u"handshake, honestly report the same 832 KB, and have a DIFFERENT memory "
              u"map. You cannot tell them apart by looking. Code uploaded into one of those "
              u"would execute against the wrong addresses.\n\n"
              u"Turning it off makes sense if you know your ECU is compatible — from the "
              u"list in the documentation, say. Everything beyond that is on you.\n\n"
              u"Turn it off until the program is closed?",
    },
    "st.force_on": {
        "ru": u"Проверка блока ОТКЛЮЧЕНА. Запись пойдёт в любой ЭБУ, который ответит. "
              u"Действует до закрытия программы.\n",
        "en": u"The ECU check is OFF. A write will go into any ECU that answers. This lasts "
              u"until the program is closed.\n",
    },
    "st.force_off": {
        "ru": u"Проверка блока снова включена.\n",
        "en": u"The ECU check is on again.\n",
    },
    "ui.text_files": {"ru": u"Текст", "en": u"Text"},
    "ui.image_files": {"ru": u"Образ прошивки", "en": u"Firmware image"},
    "ui.all_files": {"ru": u"Все файлы", "en": u"All files"},

    # --- status line ------------------------------------------------------
    "st.ready": {
        "ru": u"Готов. Подключите адаптер и выберите файл.",
        "en": u"Ready. Connect the adapter and choose a file.",
    },
    "st.files_here": {
        "ru": u"Файлы программы (журнал, настройки, бэкапы) — рядом с ней: %s\n",
        "en": u"The program's files (log, settings, backups) live beside it: %s\n",
    },
    "st.files_elsewhere": {
        "ru": u"Рядом с программой писать нельзя, файлы идут в %s\n",
        "en": u"Cannot write beside the program, so its files go to %s\n",
    },
    "st.copied": {
        "ru": u"Журнал скопирован в буфер обмена.",
        "en": u"Log copied to the clipboard.",
    },
    "st.saved": {"ru": u"Журнал сохранён: %s", "en": u"Log saved: %s"},
    "st.no_adapter": {
        "ru": u"Адаптер не найден. Подключите кабель и нажмите «Обновить».",
        "en": u"No adapter found. Connect the cable and press Refresh.",
    },
    "st.reading": {"ru": u"Чтение образа из ЭБУ", "en": u"Reading the image from the ECU"},
    "st.writing": {"ru": u"Запись образа в ЭБУ", "en": u"Writing the image to the ECU"},
    "st.done_verified": {"ru": u"Готово, проверено.", "en": u"Done, verified."},
    # A write that met a region nothing can store bytes in has NOT done what was asked, and
    # "Done, verified" would be the green line this project exists to not print.
    "st.done_unplaced": {
        "ru": u"Записано и проверено, но %d участ. не принимают запись: %d байт образа "
              u"на блок не легли. Подробности в журнале.",
        "en": u"Written and verified, but %d region(s) accept nothing: %d byte(s) of the "
              u"image are not on the ECU. The log has the detail.",
    },
    # A read is NOT verified: the streaming dump carries no checksum and the only check is
    # its total length.  Saying "verified" there claimed something nobody had done.
    "st.read_done": {
        "ru": u"Готово: образ прочитан целиком. Сверка с ЭБУ не выполнялась — это потоковое "
              u"чтение, его нечем проверить на лету.",
        "en": u"Done: the image was read in full. It has not been verified against the ECU — "
              u"a streaming read carries nothing to check it against.",
    },
    "st.not_finished": {
        "ru": u"Операция не завершилась. Смотрите журнал ниже и раздел «Если не работает».",
        "en": u"The operation did not complete. See the log below and the "
              u"“If it does not work” section.",
    },
    "st.resume_armed": {
        "ru": u" Режим продолжения включён автоматически.",
        "en": u" Resume mode has been switched on automatically.",
    },
    "st.write_unfinished": {
        "ru": u"Запись не завершена. Режим продолжения включён автоматически — запустите ту "
              u"же запись снова, уже записанные секторы будут пропущены.",
        "en": u"The write did not finish. Resume mode is on — run the same write again and "
              u"the sectors already written will be skipped.",
    },
    "st.backup_at": {"ru": u"→ бэкап: %s\n", "en": u"→ backup: %s\n"},
    "st.unwritable": {
        "ru": u"→ 0x%06X: не записывается (не флеш)",
        "en": u"→ 0x%06X: cannot be written (not flash)",
    },
    "st.unwritable_bad": {
        "ru": u" И ОБРАЗ ХОЧЕТ ДРУГОГО",
        "en": u" AND THE IMAGE WANTS DIFFERENT BYTES",
    },
    "st.protection_installed": {
        "ru": u"→ на этом ЭБУ установлена защита флеша от записи",
        "en": u"→ this ECU has flash write protection installed",
    },
    # --flash never writes the boot sector.  If the image carries a different one the ECU
    # ends up running that image's application under its own existing loader, which is a
    # thing to be told during the run rather than to work out afterwards.
    # The tool has bounded what it can do and is going ahead anyway.  That belongs in the
    # window, not only in the log: it changes what the user should expect to see.  The two
    # rate-decision problems (rate_unconfirmed, rate_unverified) deliberately stay in the
    # log -- they are transport-internal, they can fire several times in a run, and the
    # user's decision does not change because of them.
    "st.stopping": {
        "ru": u"Останавливаем запись на границе сектора…",
        "en": u"Stopping the write at a sector boundary…",
    },
    "st.link_below_target": {
        "ru": u"→ связь хуже, чем нужно для записи страниц: идём на самой медленной "
              u"скорости, повторы ожидаемы",
        "en": u"→ the link is below what page programming wants: running at the slowest "
              u"rate, expect resends",
    },
    "st.boot_differs": {
        "ru": u"→ загрузочный сектор образа отличается (%d байт) и НЕ записывается",
        "en": u"→ the image's boot sector differs (%d bytes) and is NOT being written",
    },

    # --- progress rendered from engine events -----------------------------
    "op.read": {"ru": u"Чтение", "en": u"Reading"},
    "op.backup": {"ru": u"Снятие бэкапа", "en": u"Backing up"},
    "op.verify": {"ru": u"Итоговая сверка", "en": u"Final verify"},
    "op.write": {"ru": u"Запись", "en": u"Writing"},
    "pr.sectors": {
        "ru": u"%s: сектор %d из %d%s%s",
        "en": u"%s: sector %d of %d%s%s",
    },
    "pr.bytes": {
        "ru": u"%s: %d%% (%d из %d КБ)%s",
        "en": u"%s: %d%% (%d of %d KB)%s",
    },
    "pr.retries": {"ru": u", повторов %d", "en": u", %d retries"},
    "pr.eta": {"ru": u", осталось %d:%02d", "en": u", %d:%02d left"},

    # --- coarse stages ----------------------------------------------------
    "sg.handshake": {
        "ru": u"ЭБУ ответил, загрузчик активен.",
        "en": u"The ECU answered; the loader is live.",
    },
    "sg.handoff": {
        "ru": u"Программа-агент передана в ЭБУ.",
        "en": u"The agent program has been delivered to the ECU.",
    },
    "sg.calibrated": {
        "ru": u"Стенд измерен, скорость и число повторов подобраны.",
        "en": u"The bench has been measured; speed and retry count chosen.",
    },
    "sg.backup": {
        "ru": u"Снимаю полный бэкап до первой стёртой ячейки…",
        "en": u"Taking a full backup before a single byte is erased…",
    },
    "sg.backup_saved": {"ru": u"Бэкап сохранён.", "en": u"Backup saved."},
    "sg.writing": {"ru": u"Записываю…", "en": u"Writing…"},
    "sg.verifying": {"ru": u"Сверяю записанное…", "en": u"Verifying what was written…"},
    "sg.power_cycled": {
        "ru": u"Питание ЭБУ передёрнуто автоматически.",
        "en": u"The ECU's power was cycled automatically.",
    },

    # --- verdicts ---------------------------------------------------------
    "vd.handshake": {
        "ru": u"Блок не ответил. Чаще всего +12 на A4/B2 подано ПОСЛЕ питания, а надо ДО. "
              u"Передёрните питание и попробуйте снова.",
        "en": u"The ECU did not answer. Most often +12 V reached A4/B2 AFTER main power "
              u"instead of before. Cycle the power and try again.",
    },
    "vd.link": {
        "ru": u"Связь оборвалась. Запустите ту же операцию снова — запись продолжится с "
              u"места остановки.",
        "en": u"The link was lost. Run the same operation again — the write continues from "
              u"where it stopped.",
    },
    "vd.read": {
        "ru": u"Чтение не завершено: поток встал, образ неполный. Передёрните питание и "
              u"повторите.",
        "en": u"The read did not finish: the stream stalled and the image is incomplete. "
              u"Cycle the power and repeat.",
    },
    "vd.generic": {
        "ru": u"Защита сработала, работа остановлена. Смотрите журнал.",
        "en": u"A safety check stopped the run. See the log.",
    },

    # --- dialogs ----------------------------------------------------------
    "dl.no_adapter": {"ru": u"Адаптер не выбран.", "en": u"No adapter selected."},
    "dl.pick_image": {
        "ru": u"Выберите файл образа для записи.",
        "en": u"Choose an image file to write.",
    },
    "dl.wrong_size": {
        "ru": u"Файл должен быть ровно %d байт (832 КБ).\nВыбранный файл: %d байт.",
        "en": u"The file must be exactly %d bytes (832 KB).\nThe chosen file is %d bytes.",
    },
    "dl.power_title": {"ru": u"Передёрните питание ЭБУ", "en": u"Cycle the ECU's power"},
    "dl.power_body": {
        "ru": u"Снимите +12 с X2:H1/H2 и подайте снова.\n\nУбедитесь, что +12 на X1:A4 и "
              u"X1:B2 подано ДО основного питания.\n\nЗатем нажмите OK — программа сразу "
              u"начнёт работу.",
        "en": u"Remove +12 V from X2:H1/H2 and apply it again.\n\nMake sure +12 V reaches "
              u"X1:A4 and X1:B2 BEFORE main power.\n\nThen press OK — the program starts "
              u"immediately.",
    },
    "dl.quit_title": {"ru": u"Идёт запись", "en": u"A write is in progress"},
    "dl.quit_body": {
        "ru": u"Сейчас идёт запись в ЭБУ. Если закрыть программу, запись оборвётся на "
              u"середине и блок не запустится, пока её не довести до конца.\n\n"
              u"Закрыть всё равно?",
        "en": u"A write to the ECU is in progress. Closing now interrupts it midway and the "
              u"ECU will not run until the write is completed.\n\nClose anyway?",
    },
    "dl.confirm_title": {"ru": u"Подтверждение записи", "en": u"Confirm the write"},
    "dl.confirm_body": {
        "ru": u"Будет перезаписана прошивка ЭБУ.\n\nРежим: %s\n\nПеред записью программа "
              u"снимет полный бэкап текущей прошивки, и без\nнего запись не начнётся. "
              u"Загрузочный сектор не затрагивается.\n\nБэкап будет сохранён в:\n%s\n\n"
              u"Займёт %s. Не отключайте питание и кабель.",
        "en": u"The ECU's firmware will be overwritten.\n\nMode: %s\n\nA full backup of the "
              u"current firmware is taken first, and the write\ndoes not start without it. "
              u"The boot sector is left untouched.\n\nThe backup will be saved to:\n%s\n\n"
              u"This takes %s. Do not disconnect power or the cable.",
    },
    "dl.mode_fast": {
        "ru": u"быстро — сверка по контрольной сумме",
        "en": u"fast — verified by checksum",
    },
    "dl.mode_reliable": {
        "ru": u"надёжно — полная побайтная сверка",
        "en": u"reliable — full byte-for-byte verify",
    },
    "dl.write_time_fast": {"ru": u"около восьми минут", "en": u"about eight minutes"},
    "dl.write_time_reliable": {"ru": u"около тринадцати минут", "en": u"about thirteen minutes"},
    "dl.error": {"ru": u"ОШИБКА: %s\n", "en": u"ERROR: %s\n"},

    # --- the WHY table: raw failures translated into the action that fixes them
    "why.write_timeout": {
        "ru": u"Адаптер не отвечает. Выньте и вставьте его USB-кабель — драйвер иногда "
              u"зависает намертво.",
        "en": u"The adapter is not responding. Unplug and replug its USB cable — the driver "
              u"sometimes hangs solid.",
    },
    "why.adapter_wedged": {
        "ru": u"Адаптер завис: он виден системе, но драйвер не принимает никаких настроек. "
              u"Попробуйте другой порт USB, затем перезагрузку компьютера, затем этот же "
              u"адаптер на другой машине. Переподключение кабеля само по себе не помогает, "
              u"а передёргивание питания ЭБУ тут ни при чём.",
        "en": u"The adapter is wedged: the system still sees it, but its driver will not "
              u"accept any settings. Try a different USB port, then rebooting this machine, "
              u"then the same adapter on another computer. Replugging alone does not clear "
              u"it, and power-cycling the ECU has nothing to do with it.",
    },
    "why.no_permission": {
        "ru": u"Система не дала доступ к порту. На Linux это почти всегда группа: "
              u"sudo usermod -aG dialout $USER, затем перезайти в систему.",
        "en": u"The system refused access to the port. On Linux this is almost always "
              u"group membership: sudo usermod -aG dialout $USER, then log out and back in.",
    },
    "why.port_busy": {
        "ru": u"Порт занят другой программой или адаптер отключён.",
        "en": u"The port is held by another program, or the adapter is unplugged.",
    },
    "why.access_denied": {
        "ru": u"Порт занят другой программой. Закройте её.",
        "en": u"The port is held by another program. Close it.",
    },
    "why.no_ack": {
        "ru": u"Блок не ответил. Чаще всего +12 на A4/B2 подано ПОСЛЕ питания, а надо ДО. "
              u"Передёрните питание и попробуйте снова.",
        "en": u"The ECU did not answer. Most often +12 V reached A4/B2 AFTER main power "
              u"instead of before. Cycle the power and try again.",
    },
    "why.backup_failed": {
        "ru": u"Не удалось снять бэкап, поэтому запись не начата. Проверьте связь и "
              u"повторите.",
        "en": u"The backup could not be taken, so the write never started. Check the link "
              u"and repeat.",
    },
    "why.verify_failed": {
        "ru": u"Защита сработала: несовпадение поймано, работа остановлена. Запустите "
              u"запись снова — она перепишет проблемный сектор.",
        "en": u"A safety check fired: a mismatch was caught and the run stopped. Start the "
              u"write again and it will rewrite the sector that failed.",
    },
    "why.link_lost": {
        "ru": u"Связь оборвалась. Запустите ту же операцию снова — запись продолжится с "
              u"места остановки.",
        "en": u"The link was lost. Run the same operation again — the write continues from "
              u"where it stopped.",
    },

    # --- the drawings -----------------------------------------------------
    "dg.connectors_title": {
        "ru": u"Колодки ЭБУ, вид со стороны разъёмов",
        "en": u"The ECU's connectors, seen from the plug side",
    },
    "dg.connectors_warn": {
        "ru": u"Колодки зеркальны: у X1 буквы идут справа налево, у X2 слева направо. "
              u"Сверяйтесь с маркировкой на самой колодке.",
        "en": u"The two housings are mirrored: X1's letters run right to left, X2's left to "
              u"right. Check against the markings on the connector itself.",
    },
    "dg.x1_sub": {"ru": u"сигнальная, большая", "en": u"signal, the large one"},
    "dg.x2_sub": {"ru": u"силовая, малая", "en": u"power, the small one"},
    "dg.pinout_title": {
        "ru": u"Куда подключаться на самом ЭБУ",
        "en": u"Where to connect on the ECU itself",
    },
    "dg.pinout_warn": {
        "ru": u"У блока ДВЕ колодки, и координаты пинов на них повторяются. "
              u"X1 — сигнальная (большая), X2 — силовая (малая).",
        "en": u"The ECU has TWO connectors and the pin coordinates repeat on both. "
              u"X1 is signal (large), X2 is power (small).",
    },
    "dg.col_circuit": {"ru": u"Цепь", "en": u"Circuit"},
    "dg.col_pins": {"ru": u"Колодка и пины", "en": u"Connector and pins"},
    "dg.col_note": {"ru": u"Примечание", "en": u"Note"},

    "pin.pwr_k30": {"ru": u"+12 постоянное (K30)", "en": u"+12 V permanent (K30)"},
    "pin.pwr_k15": {"ru": u"+12 зажигание (K15)", "en": u"+12 V ignition (K15)"},
    "pin.gnd": {"ru": u"Масса (GND)", "en": u"Ground (GND)"},
    "pin.gnd_short": {"ru": u"Масса", "en": u"Ground"},
    "pin.ena": {"ru": u"Разрешение программирования", "en": u"Programming enable"},
    "pin.sig": {"ru": u"K-line (обмен)", "en": u"K-line (the link)"},
    "pin.sig_long": {"ru": u"K-line, обмен с адаптером", "en": u"K-line, to the adapter"},
    "pin.can": {"ru": u"CAN-H / CAN-L", "en": u"CAN-H / CAN-L"},

    "note.main_power": {"ru": u"основное питание", "en": u"main supply"},
    "note.j1": {"ru": u"J1 не нужен, проверено", "en": u"J1 is not needed, verified"},
    "note.not_g1": {
        "ru": u"не G1; брать с блока питания",
        "en": u"not G1; take it from the supply",
    },
    "note.before_power": {"ru": u"подать ДО питания", "en": u"apply BEFORE power"},
    "note.obd7": {"ru": u"на пин 7 разъёма OBD", "en": u"to pin 7 of the OBD connector"},
    "note.can_unused": {
        "ru": u"для прошивки не нужны",
        "en": u"not used for flashing",
    },
    # Pin coordinates are identifiers, not prose, and stay identical in both languages --
    # they are catalogued anyway so the legend has one uniform kind of entry rather than a
    # rule about which column is translatable.
    "lg.pwr_k30": {"ru": u"X2: H1, H2", "en": u"X2: H1, H2"},
    "lg.pwr_k15": {"ru": u"X2: F2", "en": u"X2: F2"},
    "lg.not_g1": {"ru": u"X2: G2, G3, G4   (не G1)", "en": u"X2: G2, G3, G4   (not G1)"},
    "lg.ena": {
        "ru": u"X1: A4 и B2   — подать ДО питания",
        "en": u"X1: A4 and B2   — apply BEFORE power",
    },
    "lg.sig": {"ru": u"X1: G3   → пин 7 OBD", "en": u"X1: G3   → OBD pin 7"},
}


# ---------------------------------------------------------------------------
# Help tabs.  Kept apart from the catalogue above because they are pages rather than
# strings: each is reviewed and translated as a whole, and mixing them into the table would
# bury every short label in the file.
HELP = [
    ("help.what", {
        "ru": u"""
%s

Программа читает и записывает внутреннюю флеш-память этого ЭБУ через K-line, на
столе. Образ — обычный файл .bin ровно 832 КБ (851 968 байт).

Содержимое образа программа не разбирает и не меняет: она работает с ним как с
набором байт. Никаких калибровок, карт и правок — только «снять целиком» и
«залить целиком».

ЧЕГО ОНА НЕ ДЕЛАЕТ

  • не трогает загрузочный сектор (0xC00000-0xC02000) — он защищён и не нужен
  • не работает с внешней микросхемой EEPROM 95160: это отдельный чип, он
    снимается программатором с прищепкой, а не через этот разъём
  • не подходит для ЭБУ других семейств — только M74 с CAN
  • не выходит в сеть: ни телеметрии, ни проверки обновлений

ПОЧЕМУ ЭТО ВООБЩЕ ВОЗМОЖНО

Внутри процессора есть загрузчик, зашитый в ПЗУ на заводе. Он не зависит от
того, что записано во флеш, и позволяет загрузить в оперативную память свой
код. Поэтому даже полностью стёртый блок поднимается этой же программой —
неудачная запись не приговор.
""",
        "en": u"""
%s

This program reads and writes the ECU's internal flash over K-line, on a bench.
An image is an ordinary .bin file of exactly 832 KB (851,968 bytes).

It does not parse or alter the contents: it treats an image as a run of bytes.
No calibrations, no maps, no edits — only "take the whole thing off" and "put
the whole thing back".

WHAT IT DOES NOT DO

  • it leaves the boot sector alone (0xC00000-0xC02000) — protected, and not needed
  • it does not touch the external 95160 EEPROM: that is a separate chip and wants
    a clip-on programmer, not this connector
  • it does not fit other ECU families — M74 with CAN only
  • it never touches the network: no telemetry, no update checks

WHY THIS IS POSSIBLE AT ALL

The microcontroller carries a loader burned into ROM at the factory. It does not
depend on what is in flash and it accepts code into RAM. That is why even a fully
erased ECU comes back through this same program — a bad write is not a verdict.
"""}),

    ("help.wiring", {
        "ru": u"""
СО СТОРОНЫ АДАПТЕРА

  пин 7      — K-line, идёт на X1:G3
  пин 4 и 5  — масса, общая с блоком
  пин 16     — +12. Без него передатчик адаптера молчит, и связи не будет

ПОРЯДОК ПОДКЛЮЧЕНИЯ

  1. всё смонтировать при снятом питании
  2. подать +12 на A4 и B2   (разрешение программирования)
  3. только теперь подать основное питание на H1/H2
  4. подать зажигание на F2
  5. запускать операцию в программе

Если A4/B2 поданы, блок молчит по CAN и не заводится — это нормально и
означает, что он сидит в загрузчике и ждёт нас.

ПИТАНИЕ И ПРОВОДА

Массу и питание берите ближе к блоку и напрямую с источника: через адаптер
бывает просадка. Провод K-line — коротким, длинный собирает помехи.
""",
        "en": u"""
ON THE ADAPTER SIDE

  pin 7      — K-line, goes to X1:G3
  pins 4, 5  — ground, common with the ECU
  pin 16     — +12 V. Without it the adapter's transmitter is mute and nothing happens

ORDER OF CONNECTION

  1. assemble everything with the power off
  2. apply +12 V to A4 and B2   (programming enable)
  3. only now apply main power to H1/H2
  4. apply ignition to F2
  5. start the operation in the program

With A4/B2 applied the ECU stays silent on CAN and does not run. That is correct:
it means the unit is sitting in the loader, waiting for us.

POWER AND WIRING

Take ground and power close to the ECU and straight from the supply — routing
through the adapter can sag. Keep the K-line wire short; a long one collects noise.
"""}),

    ("help.order", {
        "ru": u"""
ЧТЕНИЕ

  1. выбрать адаптер, нажать «Прочитать образ из ЭБУ», указать куда сохранить
  2. по подсказке передёрнуть питание блока
  3. подождать около двенадцати минут — программа сохранит файл и посчитает контрольную
     сумму

ЗАПИСЬ

  1. выбрать файл образа (ровно 832 КБ), нажать «Записать образ в ЭБУ»
  2. подтвердить, по подсказке передёрнуть питание
  3. программа сначала снимет полный бэкап текущей прошивки, затем запишет
     новую и проверит каждый сектор. Около тринадцати минут в режиме
     «надёжно», около восьми в режиме «быстро»

ПОЧЕМУ ПРОСИТ ПЕРЕДЁРНУТЬ ПИТАНИЕ

Заводской загрузчик запускается только при настоящем сбросе по питанию и
подстраивается под скорость обмена по первому же байту — ровно один раз за
сброс. Второй попытки в той же сессии не бывает, поэтому каждая операция
начинается со снятия и возврата +12 на X2:H1/H2.

Это свойство процессора, а не недоработка программы, и это проверено: сброс
по сторожевому таймеру загрузчик не поднимает — управление уходит в резидентный
загрузчик во флеше, который по K-line молчит.

СКОЛЬКО ЗАНИМАЕТ ВРЕМЯ И ПОЧЕМУ

Большую часть времени занимает не обмен и не стирание сектора (оно длится
20 миллисекунд), а программирование страниц. Оно зависит от скорости связи,
и программа выбирает её сама. На исправном стенде полная заливка вместе с
бэкапом идёт около восьми минут в режиме «быстро» и около тринадцати в
режиме «надёжно» — разница в том, насколько строго проверяется записанное.
""",
        "en": u"""
READING

  1. pick the adapter, press "Read image from the ECU", choose where to save it
  2. cycle the ECU's power when prompted
  3. wait about twelve minutes — the program saves the file and reports its checksum

WRITING

  1. choose an image file (exactly 832 KB), press "Write image to the ECU"
  2. confirm, and cycle the power when prompted
  3. the program first takes a full backup of the current firmware, then writes the
     new one and verifies every sector. About thirteen minutes in reliable mode,
     about eight in fast

WHY IT ASKS FOR A POWER CYCLE

The factory loader starts only on a true power-on reset, and it measures the line
speed from the very first byte — exactly once per reset. There is no second attempt
in the same session, so every operation begins by removing and restoring +12 V on
X2:H1/H2.

That is a property of the microcontroller rather than a shortcoming here, and it has
been measured: after a watchdog reset the loader does not answer. Where control goes
instead has not been established — most likely the resident loader in the boot sector,
which does not speak K-line, but the application would look the same from here.

HOW LONG IT TAKES, AND WHY

Most of the time goes on programming pages, not on the link and not on the sector
erase, which takes 20 milliseconds. Programming does scale with the line speed, and
the program picks that speed itself. On a healthy bench a whole image, backup
included, runs about eight minutes in fast mode and about thirteen in reliable — the
difference is how hard the written data is checked.
"""}),

    ("help.safety", {
        "ru": u"""
ЧТО СДЕЛАНО, ЧТОБЫ НЕ ИСПОРТИТЬ БЛОК

  Бэкап до записи
      Перед первой же стёртой ячейкой снимается полная копия текущей прошивки.
      Не удалось её прочитать — запись не начнётся вообще. Файл кладётся рядом
      с программой — в ту папку, что названа в первой строке журнала. В имени
      дата и время.

  Проверка каждого сектора
      Записанный сектор тут же читается обратно и сверяется побайтно. Не сошлось
      — работа останавливается сразу, а не в конце.

  Полная сверка в конце (режим «надёжно»)
      Весь образ вычитывается ещё раз и сравнивается с файлом. В режиме
      «быстро» этого шага нет — там сверка идёт посекторно, по контрольной
      сумме.

  Контроль целостности посылок
      Каждая посылка защищена 16-битной контрольной суммой, помехи ловятся,
      посылка повторяется. Испорченная посылка до флеша не доходит — а если бы
      одна на 65536 всё же прошла, её поймает посекторная сверка.

  Подстройка под ваш стенд
      Программа сама измеряет качество связи и подбирает скорость и число
      повторов. Настраивать ничего не нужно.

ЕСЛИ ЗАПИСЬ ВСЁ-ТАКИ ПРЕРВАЛАСЬ

Блок в этот момент не запустится — это ожидаемо, часть прошивки стёрта.
Ничего необратимого не произошло: заводской загрузчик в ПЗУ цел и от
содержимого флеша не зависит.

  • программа сама включит галочку «Продолжить прерванную запись». Запустите
    запись того же файла ещё раз — уже записанные секторы будут пропущены,
    работа продолжится с места обрыва
  • если сомневаетесь в файле — залейте бэкап, снятый перед записью
""",
        "en": u"""
WHAT IS IN PLACE SO THE ECU DOES NOT GET RUINED

  A backup before writing
      A full copy of the current firmware is taken before a single cell is erased.
      If it cannot be read, the write does not start at all. The file lands beside the
      program — in the folder named on the log's first line — with the date and
      time in its name.

  Every sector verified
      A written sector is read straight back and compared byte for byte. On a
      mismatch the run stops there and then, not at the end.

  A full verify at the end (reliable mode)
      The whole image is read out once more and compared against the file. Fast
      mode does not do this step: it verifies sector by sector, by checksum.

  Integrity on every transfer
      Every frame carries a 16-bit checksum, noise is caught, the frame is resent.
      A corrupted frame does not reach flash — and if one in 65536 slipped past,
      the per-sector verify catches it.

  It tunes itself to your bench
      The program measures link quality and picks the speed and the retry count.
      There is nothing to configure.

IF THE WRITE WAS INTERRUPTED ANYWAY

The ECU will not run at that point — expected, part of the firmware is erased.
Nothing irreversible has happened: the factory loader lives in ROM and does not
depend on flash contents.

  • the program switches on "Resume an interrupted write" for you. Run the same
    write again — the sectors already written are skipped and the job continues
    from where it stopped
  • if you doubt the file, flash the backup taken before the write
"""}),

    ("help.trouble", {
        "ru": u"""
«Адаптер не найден»
    Проверьте кабель и драйвер адаптера. Кнопка «Обновить» перечитывает список.

«Блок не ответил» — и так несколько раз подряд
    Самая частая причина: +12 на A4/B2 подано ПОСЛЕ основного питания, а надо
    до. Снимите всё, подайте A4/B2, затем питание.
    Вторая по частоте: плохой контакт в колодке — прижмите разъём.
    Третья: на адаптере нет +12 на пине 16.

Программа замерла, полоса прогресса стоит, ничего не происходит
    Известная беда USB-адаптеров: драйвер иногда зависает намертво, и его не
    снять даже диспетчером задач. Выньте и вставьте USB-кабель адаптера —
    программа оживёт. Данные в блоке при этом не портятся.

«Порт занят» или порт не открывается
    Порт держит другая программа — диагностический сканер, монитор порта,
    предыдущая копия этой программы. Закройте её.

Очень много сообщений о повторах в журнале
    Сами по себе повторы — норма: помехи ловятся и посылки повторяются. Но если
    их сотни, укоротите провод K-line, возьмите массу ближе к блоку, проверьте
    +12 на пине 16 адаптера и уберите провод подальше от силовых кабелей.

Запись остановилась с сообщением о несовпадении
    Это защита сработала как надо: несовпадение поймано, до конца работа не
    доведена. Запустите запись того же файла снова — она продолжит с места
    остановки и перепишет проблемный сектор.

Ничего из перечисленного не помогло
    Сохраните текст из журнала целиком: в нём есть всё для разбора — какая
    скорость выбрана, сколько было повторов и на чём именно всё встало.
""",
        "en": u"""
"No adapter found"
    Check the cable and the adapter's driver. The Refresh button re-reads the list.

"The ECU did not answer" — several times running
    The most common cause: +12 V reached A4/B2 AFTER main power instead of before.
    Take everything down, apply A4/B2, then power.
    Second most common: a poor contact in the housing — press the connector home.
    Third: no +12 V on pin 16 of the adapter.

The program is frozen, the progress bar is stuck, nothing happens
    A known USB-adapter problem: the driver sometimes hangs so hard that even the
    task manager cannot end it. Unplug and replug the adapter's USB cable and the
    program comes back. Nothing in the ECU is harmed by this.

"Port busy", or the port will not open
    Another program is holding it — a diagnostic scanner, a port monitor, an earlier
    copy of this program. Close it.

A great many retry messages in the log
    Retries are normal in themselves: noise is caught and frames are resent. But if
    there are hundreds, shorten the K-line wire, take ground closer to the ECU, check
    +12 V on pin 16 of the adapter, and move the wire away from power cables.

The write stopped with a mismatch message
    That is the safety net doing its job: a mismatch was caught and the run was not
    carried through. Start the same write again — it continues from where it stopped
    and rewrites the sector that failed.

None of the above helped
    Save the whole log text: it has everything needed to work this out — which speed
    was chosen, how many retries there were, and exactly where it stopped.
"""}),
]

HELP_TITLES = {
    "help.what": {"ru": u"Что это", "en": u"What this is"},
    "help.wiring": {"ru": u"Подключение", "en": u"Wiring"},
    "help.order": {"ru": u"Порядок работы", "en": u"How to run it"},
    "help.safety": {"ru": u"Защита от порчи", "en": u"Safety net"},
    "help.trouble": {"ru": u"Если не работает", "en": u"If it does not work"},
}


def help_tabs(device):
    """(title, body) for every help tab, in the current language."""
    out = []
    for key, bodies in HELP:
        body = bodies.get(_lang) or bodies[DEFAULT]
        if "%s" in body:
            body = body % device
        out.append((HELP_TITLES[key].get(_lang) or HELP_TITLES[key][DEFAULT], body))
    return out
