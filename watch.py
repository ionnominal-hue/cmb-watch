#!/usr/bin/env python3
"""Python 3.10+, standard library only. Never prints bot tokens or signed URLs."""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

DEFAULT_URL = 'https://disk.yandex.ru/d/QoUDDtU533bpFa'
DEFAULT_SHA = '91a90f11d33851a07df0c7a01932b4413a4cefade539df59a51c5855126dcd75'
API = 'https://cloud-api.yandex.net/v1/disk/public/resources'
LOG = logging.getLogger('watch')


def fetch(url, data=None, headers=None, timeout=30):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers or {}), timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Do not propagate URL: Telegram URLs contain the bot token.
        code = exc.code
        retry = exc.headers.get('Retry-After', '')
        exc.close()
        raise RuntimeError(f'HTTP {code}' + (f'; Retry-After={retry}' if retry.isdigit() else '')) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError('Ошибка сети или тайм-аут') from None


def json_get(url):
    with fetch(url) as r:
        return json.load(r)


def validate_zip(path, expected_size, expected_sha):
    if path.stat().st_size != expected_size:
        raise ValueError('Размер скачанного файла не совпал')
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    if expected_sha and digest.hexdigest().lower() != expected_sha.lower():
        raise ValueError('SHA-256 не совпал: это другой файл или повреждённая загрузка')
    # ZIP: full CRC check. RAR/other files: size + hash (no extraction).
    if path.suffix.lower() != '.zip' and not zipfile.is_zipfile(path):
        if path.suffix.lower() == '.rar':
            with path.open('rb') as f:
                if f.read(7) != b'Rar!\x1a\x07\x00' and not _is_rar5(path):
                    raise ValueError('Вместо RAR получено другое содержимое')
        return digest.hexdigest()
    with zipfile.ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist()) > 2 * 1024**3:
            raise ValueError('Распакованный размер превышает защитный лимит 2 ГБ')
        if z.testzip() is not None:
            raise ValueError('Повреждён ZIP (CRC)')
    return digest.hexdigest()


def _is_rar5(path):
    with path.open('rb') as f:
        return f.read(8) == b'Rar!\x1a\x07\x01\x00'


def download(url, folder, expected_sha):
    q = '?' + urllib.parse.urlencode({'public_key': url})
    LOG.info('Этап: метаданные Яндекса')
    meta = json_get(API + q)
    if meta.get('type') != 'file':
        raise ValueError('Ссылка должна вести на файл, а не папку')
    size = int(meta['size'])
    if not 0 < size <= 100 * 1024**2:
        raise ValueError('Размер вне лимита загрузки 100 МБ')
    name = Path(meta['name']).name
    path = folder / name
    sha = expected_sha or meta.get('sha256', '')
    if path.exists():
        try:
            return path, validate_zip(path, size, sha)
        except (ValueError, zipfile.BadZipFile):
            path.unlink()
    LOG.info('Этап: получение адреса загрузки; файл %s (%s байт)', name, size)
    link = json_get(API + '/download' + q)['href']
    LOG.info('Этап: скачивание содержимого файла')
    part = folder / (name + '.part')
    started = time.monotonic()
    try:
        with fetch(link, timeout=30) as response, part.open('wb') as dest:
            if response.status != 200:
                raise ValueError('Неполный HTTP-ответ вместо файла')
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > size or time.monotonic() - started > 180:
                    raise ValueError('Превышен размер или время загрузки')
                dest.write(chunk)
        digest = validate_zip(part, size, sha)
        part.replace(path)
        return path, digest
    finally:
        part.unlink(missing_ok=True)


def telegram(token, method, fields, document=None):
    if document:
        boundary = 'watch' + uuid.uuid4().hex
        body = bytearray()
        for key, value in fields.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="archive{document.suffix}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
        body.extend(document.read_bytes())
        body.extend(f'\r\n--{boundary}--\r\n'.encode())
        data = bytes(body)
        headers = {'Content-Type': 'multipart/form-data; boundary=' + boundary}
    else:
        data = urllib.parse.urlencode(fields).encode()
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    with fetch(f'https://api.telegram.org/bot{token}/{method}', data, headers, timeout=120) as r:
        answer = json.load(r)
    if not answer.get('ok'):
        raise RuntimeError('Telegram отклонил запрос; проверьте токен, chat_id и права бота')
    return answer['result']


def save_state(path, state):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)
    if os.getenv('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write('changed=true\n')


def check(args):
    folder = Path(args.data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    identity = hashlib.sha256((args.url + args.sha256 + args.chat).encode()).hexdigest()
    if state.get('identity') != identity:
        state = {'identity': identity}
    if state.get('notified') and (not args.send_file or state.get('file_sent')):
        LOG.info('Уже доставлено. Проверка не требуется.')
        return True
    cached = state.get('downloaded', {})
    path = folder / Path(cached.get('name', '__missing__')).name
    if cached and path.is_file():
        digest = validate_zip(path, cached['size'], cached['sha256'])
    else:
        path, digest = download(args.url, folder, args.sha256)
    state['downloaded'] = {'name': path.name, 'size': path.stat().st_size, 'sha256': digest}
    LOG.info('Архив полностью скачан и проверен: %s (%s байт)', path.name, path.stat().st_size)
    # Save before Telegram: a subsequent run can reuse the verified file.
    save_state(state_path, state)
    if not state.get('notified'):
        telegram(args.token, 'sendMessage', {'chat_id': args.chat, 'text':
            f'✅ Файл скачан: {path.name}\nРазмер и SHA-256 проверены. Для ZIP также проверен CRC; для RAR проверка CRC не выполняется.\n{args.url}\nSHA-256: {digest}'})
        state['notified'] = True
        save_state(state_path, state)
    if args.send_file and not state.get('file_sent'):
        if path.stat().st_size > 50_000_000:
            LOG.warning('Файл сохранён, но превышает лимит отправки 50 МБ. Уведомление доставлено.')
            state['file_sent'] = True  # no endless retries for an oversized file
            state['file_skipped_too_large'] = True
        else:
            telegram(args.token, 'sendDocument', {'chat_id': args.chat, 'caption':
                'Файл скачан с Яндекс Диска; размер и SHA-256 проверены.'}, path)
            state['file_sent'] = True
        save_state(state_path, state)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-network', action='store_true', help='Скачать открытый тестовый файл без Telegram')
    parser.add_argument('--test-download', action='store_true', help='Скачать и проверить ZIP Яндекса без Telegram')
    parser.add_argument('--once', action='store_true', help='Одна проверка (cron/GitHub Actions)')
    parser.add_argument('--test-telegram', action='store_true', help='Отправить тестовое сообщение без проверки Яндекса')
    parser.add_argument('--chat-id', action='store_true', help='Показать chat_id из сообщений, отправленных боту')
    parser.add_argument('--url', default=os.getenv('PUBLIC_URL', DEFAULT_URL))
    parser.add_argument('--interval', type=int, default=int(os.getenv('CHECK_INTERVAL', '60')))
    parser.add_argument('--data-dir', default=os.getenv('DATA_DIR', 'data'))
    parser.add_argument('--sha256', default=os.getenv('EXPECTED_SHA256'))
    parser.add_argument('--send-file', action=argparse.BooleanOptionalAction,
                        default=os.getenv('SEND_FILE', 'true').lower() in ('1', 'true', 'yes'))
    args = parser.parse_args()
    if args.sha256 is None:
        args.sha256 = DEFAULT_SHA if args.url.rstrip('/').split('/')[-1] in ('QoUDDtU533bpFa', 'MfLFg8_suAbHmw') else ''
    args.token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    args.chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    LOG.info('Проверяемая публичная ссылка: %s', args.url)
    if os.getenv('PUBLIC_URL'):
        LOG.info('Адрес взят из PUBLIC_URL (приоритет над DEFAULT_URL; --url имеет высший приоритет).')
    if args.test_network or args.test_download:
        try:
            folder = Path(args.data_dir)
            folder.mkdir(parents=True, exist_ok=True)
            if args.test_network:
                with fetch('https://raw.githubusercontent.com/github/gitignore/main/Python.gitignore') as response:
                    data = response.read(1024 * 1024)
                if b'__pycache__' not in data:
                    raise ValueError('Неожиданное содержимое тестового файла')
                (folder / 'network-test.txt').write_bytes(data)
                LOG.info('SUCCESS: тестовый файл скачан, %s байт. Исходящий HTTPS работает.', len(data))
            if args.test_download:
                path, digest = download(args.url, folder, args.sha256)
                LOG.info('SUCCESS: файл скачан и проверен: %s, %s байт, SHA-256 %s', path, path.stat().st_size, digest)
        except Exception as exc:
            LOG.error('FAILED: %s', str(exc)[:300])
            raise SystemExit(1)
        return
    if not args.token:
        parser.error('Задайте TELEGRAM_BOT_TOKEN в переменных окружения')
    if args.chat_id:
        result = telegram(args.token, 'getUpdates', {'timeout': 0})
        chats = {str(u['message']['chat']['id']) for u in result if 'message' in u}
        print('\n'.join(sorted(chats)) if chats else 'Отправьте /start боту и повторите в течение 24 часов.')
        return
    if not args.chat:
        parser.error('Задайте TELEGRAM_CHAT_ID в переменных окружения')
    if args.test_telegram:
        telegram(args.token, 'sendMessage', {'chat_id': args.chat, 'text': '✅ Проверка связи: бот CombatCraft настроен.'})
        return
    if args.interval < 60:
        parser.error('Минимальный интервал — 60 секунд')
    backoff = args.interval
    while True:
        started = time.monotonic()
        try:
            if check(args):
                LOG.info('Готово. Скрипт завершён; файлы остались в %s', args.data_dir)
                return
        except Exception as exc:
            # Redact token and avoid logging URLs from underlying exceptions.
            msg = str(exc).replace(args.token, '[REDACTED]')
            if 'https://' in msg or 'http://' in msg:
                msg = type(exc).__name__ + ': ошибка запроса'
            LOG.warning('Пока не удалось: %s', msg[:300])
            if 'HTTP 429' in msg:
                backoff = min(max(backoff * 2, 300), 3600)
            else:
                backoff = args.interval
        if args.once:
            return
        time.sleep(max(0, backoff - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
