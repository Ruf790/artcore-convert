# ArtCore Convert

Видеоконвертер для Windows на **FFmpeg** с интерфейсом на Qt. Четвёртая программа семейства ArtCore — рядом с ArtCore Client, [ArtCore Player](https://github.com/Ruf790/artcore-player) и [ArtCore Music](https://github.com/Ruf790/artcore-music).

*A video converter for Windows on FFmpeg with a Qt interface. English below.*

## Возможности

- **Кодирование на видеокарте:** NVIDIA **NVENC** (H.264, HEVC, AV1 — RTX 40/50), AMD **AMF**, Intel **Quick Sync**. При запуске программа сама проверяет, какие кодеры работают на этом ПК (кодирует несколько кадров), и в режиме «авто» берёт лучший: NVIDIA → AMD → Intel → процессор.
- **Кодирование на процессоре:** x264, x265, SVT-AV1.
- **Качество:** постоянное качество (CQ / CRF / QP), битрейт или **размер файла** (битрейт считается сам); ползунок «быстрее ↔ качественнее» для любого кодера; 8 или 10 бит; **HDR10 / HLG сохраняются** при кодировании в HEVC / AV1 10 бит.
- **Картинка:** уменьшение до 2160p…480p (только уменьшение), частота кадров, деинтерлейс (авто — для чересстрочного видео).
- **Звук:** копировать как есть или AAC, AC-3, E-AC-3, Opus, FLAC, MP3; стерео / 5.1; выравнивание громкости (EBU R128). «Авто» копирует звук, если он подходит контейнеру, иначе — AAC.
- **Дорожки:** у каждого файла можно убрать любые звуковые дорожки и субтитры или выбрать для дорожки свой кодек. В MP4 текстовые субтитры переводятся в MP4 Text.
- **Без перекодирования:** перепаковать MKV ⇄ MP4, оставить видео как есть и поменять только звук (например, DTS → AC-3 640 для телевизора).
- **Очередь:** файлы и папки (перетаскиванием или из меню Проводника «Конвертировать в ArtCore Convert»), у каждого файла свои настройки, пресеты (встроенные и свои), пауза / продолжение, остановка, повтор, приоритет ffmpeg, после очереди — открыть папку / сон / выключить ПК. Очередь сохраняется до следующего запуска.
- **Статус:** прогресс, прошло / осталось, скорость, кадры в секунду, битрейт, размер (и прогноз), загрузка процессора и памяти, свободное место, нагрузка и температура видеокарты NVIDIA, лог ffmpeg. Прогресс виден и на кнопке в панели задач.
- **Сведения о файле:** кадр из фильма, контейнер, длительность, видео (кодек, разрешение, fps, бит, HDR / Dolby Vision, чересстрочность), звук и субтитры с языками.
- Исходник никогда не перезаписывается; готовый файл появляется только после успешного кодирования (пишется в `.part`). Автообновление с GitHub Releases, RU / EN, тёмный стиль ArtCore, portable-версия.

## Сборка

Нужны Python 3.12/3.13 (64 бит) и Inno Setup 6.3+. `build.bat` скачает FFmpeg (BtbN, GPL shared), соберёт `.exe` и установщик.
На GitHub это делает CI: каждый push собирает установщик (Actions → Artifacts), а опубликованный релиз (тег `1.0.0`) получает `ArtCoreConvert-Setup-<версия>.exe` и portable-архив.

Запуск из исходников: `pip install -r requirements.txt`, FFmpeg в `PATH` или в `ffmpeg\bin\`, затем `python run.py`.
Тесты: `pytest tests` (нужен ffmpeg с libx264 / libx265 / libsvtav1).

---

## English

**ArtCore Convert** converts videos with FFmpeg: on the graphics card (NVIDIA NVENC, AMD AMF, Intel Quick Sync — found and tested at start) or on the CPU (x264, x265, SVT-AV1). Constant quality, bit rate or target file size; 8 / 10 bit with HDR10 kept; downscaling, frame rate, deinterlacing; audio copy or AAC / AC-3 / E-AC-3 / Opus / FLAC / MP3 with stereo / 5.1 and loudness normalisation; per-file track choice; remuxing without re-encoding. A queue with presets, pause, stop, priority and «when done» actions; a status tab with speed, fps, ETA, system and GPU load and the ffmpeg log; taskbar progress; Explorer context menu; auto-update; Russian / English.

License: GNU GPL v3. Third-party components: [THIRD-PARTY.md](THIRD-PARTY.md).
