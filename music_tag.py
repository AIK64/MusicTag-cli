#!/usr/bin/env python3
"""MusicTag-style batch tagging: NetEase / QQ, embedded metadata only."""
from __future__ import annotations
import argparse
import base64
import concurrent.futures
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor'))
from mutagen.mp3 import MP3
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (TIT2, TPE1, TALB, TPE2, TDRC, TRCK, TPOS, TCON,
                         TCOM, TEXT, COMM, USLT, APIC, TXXX)

PROTECTED = ('title', 'artist', 'album')
TEXT_FIELDS = ('year', 'track', 'disc', 'genre', 'albumartist', 'composer', 'lyricist', 'comment')
FIELDS = TEXT_FIELDS + ('lyrics', 'cover')
FRAMES = dict(title=TIT2, artist=TPE1, album=TALB, albumartist=TPE2,
              year=TDRC, track=TRCK, disc=TPOS, genre=TCON, composer=TCOM, lyricist=TEXT)
FLAC_KEYS = dict(title='title', artist='artist', album='album', albumartist='albumartist',
                 year='date', track='tracknumber', disc='discnumber', genre='genre',
                 composer='composer', lyricist='lyricist', comment='comment', lyrics='lyrics')
SOURCES = {0: 'netease', 1: 'qq', 2: 'xiami', 3: 'kugou', 4: 'minilyrics',
           5: 'itunes', 6: 'lastfm', 7: 'musicbrainz', 8: 'vgmdb', 9: 'kuwo'}

def supports_color(stream):
    if not getattr(stream,'isatty',lambda:False)() or 'NO_COLOR' in os.environ:
        return False
    if os.name=='nt':
        try:
            import ctypes
            from ctypes import wintypes
            import msvcrt
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.GetConsoleMode.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
            kernel.GetConsoleMode.restype=wintypes.BOOL
            kernel.SetConsoleMode.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            kernel.SetConsoleMode.restype=wintypes.BOOL
            handle=wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            mode=wintypes.DWORD()
            if not kernel.GetConsoleMode(handle,ctypes.byref(mode)):
                return False
            if not kernel.SetConsoleMode(handle,mode.value | 0x0004):
                return False
        except (OSError,ValueError,AttributeError):
            return False
    return True

def status_text(text, status=None, *, file=None):
    stream=sys.stdout if file is None else file
    color={'written':'32','error':'31'}.get(status)
    if color and supports_color(stream):
        return f'\x1b[{color}m{text}\x1b[0m'
    return str(text)

def status_print(text, status=None, *, file=None):
    stream=sys.stdout if file is None else file
    print(status_text(text,status,file=stream),file=stream,flush=True)

def json_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def norm(s):
    s = unicodedata.normalize('NFKC', html.unescape(str(s))).casefold()
    return ''.join(c for c in s if c.isalnum())

def blank(value):
    return value is None or value == '' or value == []

def load_audio(path):
    if Path(path).suffix.lower() == '.mp3':
        return MP3(path)
    if Path(path).suffix.lower() == '.flac':
        return FLAC(path)
    raise ValueError('仅支持 MP3 / FLAC；NCM 请先转换')

def read_tags(path):
    a = load_audio(path)
    result = {'duration': a.info.length, 'title': [], 'artist': [], 'album': []}
    if isinstance(a, MP3):
        t = a.tags
        for key, cls in FRAMES.items():
            result[key] = [str(x) for f in (t.getall(cls.__name__) if t else []) for x in f.text]
        result['comment'] = '\n'.join(str(x) for f in (t.getall('COMM') if t else []) for x in f.text)
        result['lyrics'] = '\n'.join(str(f.text) for f in (t.getall('USLT') if t else []))
        pictures = t.getall('APIC') if t else []
        result['cover'] = [{'bytes': len(p.data), 'mime': p.mime, 'type': int(p.type),
                            'sha256': hashlib.sha256(p.data).hexdigest()} for p in pictures]
        for field in ('NETEASE_SONG_ID', 'QQ_SONG_MID'):
            f = t.get('TXXX:' + field) if t else None
            result[field] = str(f.text[0]) if f and f.text else ''
    else:
        for key, tag in FLAC_KEYS.items():
            result[key] = list(a.get(tag, []))
        if not result.get('lyrics'):
            result['lyrics'] = list(a.get('unsyncedlyrics', []))
        result['cover'] = [{'bytes': len(p.data), 'mime': p.mime, 'type': int(p.type),
                            'sha256': hashlib.sha256(p.data).hexdigest()} for p in a.pictures]
        for field in ('NETEASE_SONG_ID', 'QQ_SONG_MID'):
            result[field] = next(iter(a.get(field, [])), '')
    return result

def source_settings(path=None):
    settings = dict(sources=['netease', 'qq'], workers=5, translated=True, skip_instrumental=True,
                    id3_version=3, fields=list(FIELDS))
    if path:
        root = ET.parse(path).getroot()
        def text(name, default=''):
            node = root.find(name)
            return node.text.strip() if node is not None and node.text else default
        for name in ('CombTagsInfo_SourceItemList', 'LyricInfo_SourceItemList', 'PictureInfo_SourceItemList'):
            rows = json.loads(text(name, '[]'))
            enabled = [SOURCES.get(x['Src'], str(x['Src'])) for x in sorted(rows, key=lambda x:x['Seq']) if x['Enabled']]
            settings[name] = enabled
        settings['sources'] = settings['CombTagsInfo_SourceItemList'] or settings['sources']
        settings['workers'] = max(1, min(5, int(text('AutoMatchTagsWebSearchThreadCount', '5'))))
        settings['translated'] = text('LyricDownload_DownloadTrans_Enable', 'True') == 'True'
        settings['skip_instrumental'] = text('DontDownloadLyricWithInstrumentInTitle', 'True') == 'True'
        settings['id3_version'] = int(text('ID3v2Version', '3'))
    return settings

class Client:
    def __init__(self, cache, timeout=15):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.lock = threading.Lock()
        self.last = 0.0
    def request(self, url, params=None, data=None, referer='https://music.163.com/', raw=False):
        if params:
            url += '?' + urllib.parse.urlencode(params)
        payload = urllib.parse.urlencode(data).encode() if data is not None else None
        key = hashlib.sha256(url.encode() + (payload or b'')).hexdigest()
        file = self.cache / (key + ('.bin' if raw else '.json'))
        if file.exists() and time.time() - file.stat().st_mtime < 86400 * 7:
            try:
                return file.read_bytes() if raw else json.loads(file.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                pass
        # Bound request rate across the five workers; no model calls.
        with self.lock:
            time.sleep(max(0, .15 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
        req = urllib.request.Request(url, data=payload, headers={
            'User-Agent': 'Mozilla/5.0', 'Referer': referer,
            **({'Content-Type': 'application/x-www-form-urlencoded'} if payload else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read(10 * 1024 * 1024 + 1)
        if len(body) > 10 * 1024 * 1024:
            raise ValueError('响应超过 10 MB 限制')
        if raw:
            value = body
        else:
            value = json.loads(body.decode('utf-8-sig'))
            if value.get('code', 0) not in (0, 200) or value.get('retcode', 0) != 0:
                raise ValueError('数据源返回错误: ' + str(value.get('code', value.get('retcode'))))
        # Per-thread temporary path avoids cache races without serializing network I/O.
        tmp = file.with_name(file.name + '.' + str(threading.get_ident()) + '.tmp')
        tmp.write_bytes(body)
        os.replace(tmp, file)
        return value

def netease_song(s):
    album = s.get('album') or s.get('al') or {}
    artists = s.get('artists') or s.get('ar') or []
    stamp = album.get('publishTime', 0)
    return dict(source='netease', id=str(s['id']), title=s['name'],
                artist=[x['name'] for x in artists], album=album.get('name', ''),
                duration=(s.get('duration') or s.get('dt') or 0) / 1000,
                year=time.strftime('%Y', time.gmtime(stamp/1000)) if stamp > 0 else '',
                track=str(s.get('no') or s.get('position') or ''), disc=str(s.get('disc') or ''),
                albumartist=[album['artist']['name']] if album.get('artist', {}).get('name') else [],
                cover_url=album.get('picUrl', ''), album_id=album.get('id'))

def qq_song(s):
    album = s.get('album') or {}
    mid = album.get('mid') or s.get('albummid')
    return dict(source='qq', id=str(s.get('mid') or s['songmid']),
                title=html.unescape(s.get('title') or s.get('songname') or s.get('name', '')),
                artist=[html.unescape(x['name']) for x in s.get('singer', [])],
                album=html.unescape(album.get('name') or s.get('albumname', '')),
                duration=s.get('interval', 0), year=(s.get('time_public') or album.get('time_public') or '')[:4],
                track=str(s.get('index_album') or s.get('belongCD') or ''),
                disc=str(s['index_cd'] + 1) if 'index_cd' in s else '',
                cover_url=f'https://y.gtimg.cn/music/photo_new/T002R800x800M000{mid}.jpg' if mid else '')

def search(client, source, title, artist, limit=10):
    query = title + ' ' + ' '.join(artist)
    if source == 'netease':
        d = client.request('https://music.163.com/api/search/get',
                           data=dict(s=query, type=1, limit=limit, offset=0))
        return [netease_song(s) for s in d.get('result', {}).get('songs', [])]
    d = client.request('https://c.y.qq.com/soso/fcgi-bin/client_search_cp',
                       params=dict(w=query, format='json', p=1, n=limit), referer='https://y.qq.com/')
    return [qq_song(s) for s in d.get('data', {}).get('song', {}).get('list', [])]

def detail(client, source, sid):
    if source == 'netease':
        d = client.request('https://music.163.com/api/song/detail/', params={'ids':json.dumps([int(sid)])})
        if not d.get('songs'):
            raise ValueError('网易云歌曲 ID 不存在')
        song = netease_song(d['songs'][0])
        # Album endpoint supplies album artist / release date if song detail omitted them.
        if song.get('album_id'):
            try:
                a = client.request(f'https://music.163.com/api/album/{song["album_id"]}').get('album', {})
                if a.get('publishTime', 0) > 0:
                    song['year'] = time.strftime('%Y', time.gmtime(a['publishTime']/1000))
                song['albumartist'] = [x['name'] for x in a.get('artists', []) if x.get('name')]
                if not song['albumartist'] and a.get('artist', {}).get('name'):
                    song['albumartist'] = [a['artist']['name']]
                if a.get('subType') and a.get('subType') not in ('录音室版', '录音室专辑'):
                    song['album_type'] = a['subType']
            except Exception as e:
                song.setdefault('warnings', []).append('专辑详情: '+str(e))
        return song
    d = client.request('https://u.y.qq.com/cgi-bin/musicu.fcg', params={'data':json.dumps({
        'req':{'module':'music.pf_song_detail_svr','method':'get_song_detail','param':{'song_mid':sid}}})},
        referer='https://y.qq.com/')
    req = d.get('req', {})
    if req.get('code') != 0 or not req.get('data', {}).get('track_info'):
        raise ValueError('QQ 歌曲详情无有效数据')
    song = qq_song(req['data']['track_info'])
    info = req['data'].get('info', [])
    mapping = {'composer':'composer', 'lyricist':'lyricist', 'genre':'genre'}
    for item in info:
        key = mapping.get(item.get('selected')) or mapping.get(item.get('type'))
        values = [x['value'].strip() for x in item.get('content', []) if x.get('value', '').strip()]
        if key and values:
            song[key] = values
    return song

TIMESTAMP = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\]')
def translated_lyrics(original, translated):
    if not translated.strip():
        return original.strip()
    bytime = {}
    def stamp(m):
        return round((int(m.group(1))*60+float(m.group(2)))*1000)
    for line in translated.splitlines():
        text = TIMESTAMP.sub('', line).strip()
        if text:
            for m in TIMESTAMP.finditer(line):
                bytime.setdefault(stamp(m), []).append(text)
    out=[]
    for line in original.splitlines():
        out.append(line)
        for m in TIMESTAMP.finditer(line):
            for text in bytime.get(stamp(m), []):
                if text != TIMESTAMP.sub('', line).strip():
                    out.append(m.group(0)+text)
    return '\n'.join(out).strip()

def lyrics(client, song, translated=True):
    if song['source'] == 'netease':
        d=client.request('https://music.163.com/api/song/lyric',params=dict(id=song['id'],lv=-1,kv=-1,tv=-1,rv=-1))
        if d.get('nolyric') or d.get('uncollected'):
            return ''
        original=d.get('lrc',{}).get('lyric','')
        trans=d.get('tlyric',{}).get('lyric','')
    else:
        d=client.request('https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg',
                         params=dict(songmid=song['id'],format='json',nobase64=1),referer='https://y.qq.com/')
        original=html.unescape(d.get('lyric',''))
        trans=html.unescape(d.get('trans',''))
    return translated_lyrics(original,trans) if translated else original.strip()

def choose(candidates, local):
    title = local['title'][0] if local['title'] else ''
    artists = {norm(x) for x in local['artist'] if norm(x)}
    album = norm(local['album'][0]) if local['album'] else ''
    good=[]
    for s in candidates:
        if norm(s['title']) != norm(title):
            continue
        if artists and artists != {norm(x) for x in s['artist']}:
            continue
        if album and norm(s['album']) != album:
            continue
        if local.get('duration') and s.get('duration') and abs(local['duration']-s['duration']) > 4:
            continue
        good.append(s)
    ids={s['id'] for s in good}
    if len(ids) == 1:
        return good[0]
    return None

def from_163key(tags):
    value=tags.get('comment', '')
    value='\n'.join(value) if isinstance(value,list) else value
    marker="163 key(Don't modify):"
    if marker not in value:
        return None
    try:
        from Crypto.Cipher import AES
        b=base64.b64decode(value.split(marker,1)[1].strip())
        dec=AES.new(b'#14ljk_!\\]&0U<\'(',AES.MODE_ECB).decrypt(b)
        dec=dec[:-dec[-1]]
        obj=json.loads(dec.decode('utf-8').removeprefix('music:'))
        return str(obj['musicId'])
    except Exception:
        return None

def ncm_id(path):
    # Only read the metadata header; do not decode or download audio.
    from Crypto.Cipher import AES
    with Path(path).open('rb') as f:
        if f.read(8)!=b'CTENFDAM':
            raise ValueError('无效 NCM 头')
        f.read(2)
        size=struct.unpack('<I',f.read(4))[0]
        if size>1024*1024:
            raise ValueError('异常 NCM 密钥长度')
        f.seek(size,1)
        size=struct.unpack('<I',f.read(4))[0]
        if size>8*1024*1024:
            raise ValueError('异常 NCM 元数据长度')
        meta=bytes(x^0x63 for x in f.read(size))
    if not meta:
        return None
    dec=AES.new(b'#14ljk_!\\]&0U<\'(',AES.MODE_ECB).decrypt(base64.b64decode(meta[22:]))
    obj=json.loads(dec[:-dec[-1]].decode('utf-8').removeprefix('music:'))
    return str(obj['musicId'])

def image_info(body):
    if body.startswith(b'\x89PNG\r\n\x1a\n') and len(body)>=24:
        return 'image/png', *struct.unpack('>II',body[16:24])
    if body.startswith(b'\xff\xd8'):
        offset=2
        while offset+4<len(body):
            if body[offset]!=255:
                offset+=1
                continue
            marker=body[offset+1]
            offset+=2
            if marker in (0xD8,0xD9) or 0xD0<=marker<=0xD7:
                continue
            size=struct.unpack('>H',body[offset:offset+2])[0]
            if size<2 or offset+size>len(body):
                break
            if marker in (0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF):
                h,w=struct.unpack('>HH',body[offset+3:offset+7])
                return 'image/jpeg',w,h
            offset+=size
    raise ValueError('封面不是有效 JPEG/PNG，拒绝写入')

def files_in(path, recursive):
    path=Path(path).resolve()
    if path.is_file():
        return [path] if path.suffix.lower() in ('.mp3','.flac') else []
    if not path.is_dir():
        raise ValueError('目录不存在: '+str(path))
    return sorted(p for p in (path.rglob('*') if recursive else path.iterdir())
                  if p.is_file() and p.suffix.lower() in ('.mp3','.flac')
                  and '.music-tag-backups' not in p.parts)

def make_entry(path, args, settings, client, ncm_lookup, assets):
    entry={'path':str(path),'status':'error','warnings':[]}
    try:
        local=read_tags(path)
        entry['before']=local
        entry['sha256']=digest(path)
        if not local['title'] or not local['artist']:
            entry.update(status='review',reason='缺少标题/艺术家，无法可靠自动匹配；请先补上基础标签')
            return entry
        sid=args.id or local.get('NETEASE_SONG_ID') or from_163key(local)
        id_source=args.source or 'netease'
        if not sid and local.get('QQ_SONG_MID'):
            sid=local['QQ_SONG_MID']; id_source='qq'
        if not sid and ncm_lookup.get(path.stem):
            matches=ncm_lookup[path.stem]
            if len(matches)==1:
                sid=ncm_id(matches[0]); id_source='netease'
            else:
                entry['warnings'].append('同名 NCM 不止一个，改用严格搜索匹配')
        chosen={}
        lookup_sources=list(dict.fromkeys(settings['sources']+settings.get('LyricInfo_SourceItemList',[])+settings.get('PictureInfo_SourceItemList',[])))
        if sid and id_source not in lookup_sources:
            sid=None
        for source in lookup_sources:
            try:
                if sid and source==id_source:
                    song=detail(client,source,sid)
                    # ID is strongest evidence, but still refuse a different title/artist/version.
                    if norm(song['title'])!=norm(local['title'][0]) or {norm(x) for x in song['artist']}!={norm(x) for x in local['artist']}:
                        raise ValueError('歌曲 ID 与本地标题/艺术家不一致')
                    if song.get('duration') and abs(song['duration']-local['duration'])>4:
                        raise ValueError('歌曲 ID 与本地时长不一致')
                else:
                    candidates=search(client,source,local['title'][0],local['artist'])
                    song=choose(candidates,local)
                    if not song:
                        entry.setdefault('candidates',{})[source]=candidates
                        continue
                    song=detail(client,source,song['id'])
                chosen[source]=song
                entry['warnings'].extend(song.get('warnings',[]))
            except Exception as e:
                entry['warnings'].append(source+': '+str(e))
        if not chosen:
            entry.update(status='review',reason='没有唯一且版本一致的匹配，未计划写入')
            return entry
        changes={}; provenance={}
        for field in TEXT_FIELDS:
            for source in settings['sources']:
                value=chosen.get(source,{}).get(field)
                if not blank(value):
                    values=value if isinstance(value,list) else [value]
                    values=[str(v) for v in values if not blank(v)]
                    old=local.get(field,[])
                    old=old if isinstance(old,list) else [old]
                    if values!=old:
                        changes[field]=values; provenance[field]=source
                    break
        instrumental=settings['skip_instrumental'] and bool(re.search(r'instrumental|off[\s_-]*vocal|伴奏|纯音乐',local['title'][0],re.I))
        if instrumental:
            entry['warnings'].append('伴奏/纯音乐：保留原歌词，不获取新歌词')
        else:
            for source in settings.get('LyricInfo_SourceItemList',settings['sources']):
                if source not in chosen:
                    continue
                try:
                    value=lyrics(client,chosen[source],settings['translated'])
                    if value.strip():
                        old=local.get('lyrics',[])
                        old='\n'.join(old) if isinstance(old,list) else old
                        if value!=old:
                            changes['lyrics']=value; provenance['lyrics']=source
                        break
                except Exception as e:
                    entry['warnings'].append(source+' 歌词: '+str(e))
        for source in settings.get('PictureInfo_SourceItemList',settings['sources']):
            url=chosen.get(source,{}).get('cover_url')
            if not url:
                continue
            try:
                body=client.request(url.replace('http://','https://'),raw=True,
                                    referer='https://y.qq.com/' if source=='qq' else 'https://music.163.com/')
                mime,w,h=image_info(body)
                sha=hashlib.sha256(body).hexdigest()
                dest=assets/(sha+('.jpg' if mime=='image/jpeg' else '.png'))
                if not dest.exists():
                    dest.write_bytes(body)
                old=local.get('cover',[])
                if len(old)!=1 or old[0]['sha256']!=sha or old[0]['type']!=3:
                    changes['cover']={'asset':dest.name,'sha256':sha,'mime':mime,'width':w,'height':h}
                    provenance['cover']=source
                break
            except Exception as e:
                entry['warnings'].append(source+' 封面: '+str(e))
        # Stable identifiers avoid searching again on later runs; separate from protected fields/comments.
        for source,song in chosen.items():
            key='NETEASE_SONG_ID' if source=='netease' else 'QQ_SONG_MID'
            if local.get(key)!=song['id']:
                changes[key]=song['id']; provenance[key]=source
        entry.update(status='ready' if changes else 'unchanged',changes=changes,provenance=provenance,
                     matches={s:{k:v for k,v in song.items() if k not in ('warnings',)} for s,song in chosen.items()})
        entry['unavailable']=[f for f in TEXT_FIELDS if all(blank(s.get(f)) for s in chosen.values())]
    except Exception as e:
        entry.update(status='error',reason=str(e))
    return entry

def put_tags(path, changes, assets, id3_version):
    a=load_audio(path)
    if isinstance(a,MP3):
        if a.tags is None:
            a.add_tags()
        t=a.tags
        for field,value in changes.items():
            if field in PROTECTED:
                raise ValueError('禁止修改基础字段: '+field)
            if field in FRAMES:
                cls=FRAMES[field]; t.delall(cls.__name__); t.add(cls(encoding=1,text=value))
            elif field=='comment':
                t.delall('COMM'); t.add(COMM(encoding=1,lang='eng',desc='',text=value))
            elif field=='lyrics':
                t.delall('USLT'); t.add(USLT(encoding=1,lang='xxx',desc='',text=value))
            elif field=='cover':
                body=(assets/value['asset']).read_bytes()
                t.delall('APIC'); t.add(APIC(encoding=1,mime=value['mime'],type=3,desc='Cover',data=body))
            elif field in ('NETEASE_SONG_ID','QQ_SONG_MID'):
                t.delall('TXXX:'+field); t.add(TXXX(encoding=1,desc=field,text=[value]))
            else:
                raise ValueError('未知字段: '+field)
        # Preserve an existing ID3v2.4 tag rather than downgrade unrelated v2.4 frames.
        version=4 if t.version[1]==4 else id3_version
        a.save(v2_version=version,v1=1)
    else:
        for field,value in changes.items():
            if field in PROTECTED:
                raise ValueError('禁止修改基础字段: '+field)
            if field=='cover':
                p=Picture(); p.type=3; p.mime=value['mime']; p.width=value['width']; p.height=value['height']
                p.data=(assets/value['asset']).read_bytes()
                a.clear_pictures(); a.add_picture(p)
            elif field in FLAC_KEYS:
                a[FLAC_KEYS[field]]=value if isinstance(value,list) else [value]
                if field=='lyrics' and 'unsyncedlyrics' in a:
                    del a['unsyncedlyrics']
            elif field in ('NETEASE_SONG_ID','QQ_SONG_MID'):
                a[field]=[value]
            else:
                raise ValueError('未知字段: '+field)
        a.save()

def validate_changes(before, after, changes):
    for field in PROTECTED:
        if before[field]!=after[field]:
            raise ValueError('受保护字段发生变化: '+field)
    if abs(before['duration']-after['duration'])>.001:
        raise ValueError('音频时长发生变化')
    for field,value in changes.items():
        actual=after.get(field)
        if field=='cover':
            if len(actual)!=1 or actual[0]['sha256']!=value['sha256'] or actual[0]['type']!=3:
                raise ValueError('封面写入验证失败')
        elif field=='lyrics':
            actual='\n'.join(actual) if isinstance(actual,list) else actual
            if actual!=value:
                raise ValueError('歌词写入验证失败')
        elif field=='comment':
            actual=actual if isinstance(actual,list) else [actual]
            if actual!=value:
                raise ValueError('注释写入验证失败')
        elif actual!=value:
            raise ValueError('字段写入验证失败: '+field)

def apply_plan(path=None, keep_backups=False, *, plan=None, assets=None, report=None):
    if plan is None:
        path=Path(path).resolve()
        plan=json.loads(path.read_text(encoding='utf-8-sig'))
        assets=path.parent/(path.stem+'.assets')
    if plan.get('schema')!=1:
        raise ValueError('不支持的计划版本')
    assets=Path(assets)
    result=[]
    created_backups=[]
    for entry in plan['entries']:
        row={'path':entry['path'],'status':'skipped'}
        temp=None
        backup_created=False
        try:
            if entry['status']!='ready':
                row['reason']=entry.get('reason',entry['status'])
                status_print('未修改: '+Path(entry['path']).name+' | '+row['reason'],
                             'error' if entry['status']=='error' else None)
                result.append(row); continue
            p=Path(entry['path'])
            changes=entry['changes']
            if any(f in changes for f in PROTECTED):
                raise ValueError('计划包含受保护字段')
            cover=changes.get('cover')
            if cover:
                name=Path(cover['asset'])
                if name.name!=cover['asset'] or not name.name:
                    raise ValueError('非法封面资源路径')
                body=(assets/name).read_bytes()
                if hashlib.sha256(body).hexdigest()!=cover['sha256']:
                    raise ValueError('封面资源已变动')
                image_info(body)
            if digest(p)!=entry['sha256']:
                raise ValueError('文件在预览后已改变，请重新生成计划')
            before=read_tags(p)
            fd,tmp=tempfile.mkstemp(prefix='.music-tag-',suffix=p.suffix,dir=p.parent)
            os.close(fd); temp=Path(tmp)
            shutil.copy2(p,temp)
            put_tags(temp,changes,assets,plan['settings']['id3_version'])
            after=read_tags(temp)
            validate_changes(before,after,changes)
            backup_dir=p.parent/'.music-tag-backups'
            backup_dir.mkdir(exist_ok=True)
            backup=backup_dir/(p.name+'.'+entry['sha256'][:16]+'.bak')
            if backup.exists():
                if digest(backup)!=entry['sha256']:
                    raise ValueError('已有备份校验失败，拒绝覆盖')
            else:
                shutil.copy2(p,backup)
                backup_created=True
            row.update(backup=str(backup),backup_retained=True)
            # Detect external edits while the staged copy was being written.
            if digest(p)!=entry['sha256']:
                raise ValueError('写入前原文件发生变化，已停止')
            os.replace(temp,p); temp=None
            # Verify the final file too before allowing this batch's backup to be cleaned.
            validate_changes(before,read_tags(p),changes)
            row.update(status='written',fields=list(changes),backup=str(backup),sha256=digest(p),backup_retained=True)
            if backup_created:
                created_backups.append((row,backup,entry['sha256']))
            status_print('已写入: '+p.name,'written')
        except Exception as e:
            row.update(status='error',reason=str(e)); status_print('失败: '+entry['path']+' | '+str(e),'error')
        finally:
            if temp is not None and temp.exists():
                try:
                    temp.unlink()
                except OSError as e:
                    row['cleanup_error']='临时文件清理失败: '+str(e)
        result.append(row)
    if not keep_backups:
        for row,backup,expected in created_backups:
            try:
                # Only remove backups created by this batch, for verified successful files.
                if digest(backup)!=expected:
                    raise ValueError('备份被其他程序修改，已保留')
                backup.unlink()
                row['backup_retained']=False
                row['backup_cleaned']=True
                try:
                    backup.parent.rmdir()  # Remove only the now-empty folder; never recursively.
                except OSError:
                    pass
            except (OSError,ValueError) as e:
                row['cleanup_error']='备份清理失败: '+str(e)
                status_print(row['cleanup_error'],'error')
    if report is not None:
        json_write(report,result)
        print('结果: '+str(report))
    counts={name:sum(r['status']==name for r in result) for name in ('written','skipped','error')}
    summary=(status_text(f"已写入 {counts['written']}",'written' if counts['written'] else None)
             +f"，跳过 {counts['skipped']}，"
             +status_text(f"失败 {counts['error']}",'error' if counts['error'] else None))
    print('处理结果：'+summary+'。',flush=True)
    if any(x['status']=='error' or x.get('cleanup_error') for x in result):
        return 1
    return 2 if any(e['status'] in ('review','error') for e in plan['entries']) else 0

def main():
    parser=argparse.ArgumentParser(description='批量自动匹配 MP3/FLAC 标签（网易云→QQ，全部内嵌）')
    sub=parser.add_subparsers(dest='command',required=True)
    inspect=sub.add_parser('inspect',help='读取已有标签，不修改文件'); inspect.add_argument('path')
    settings_parser=sub.add_parser('settings',help='查看从 MusicTag 读取的数据源配置')
    settings_parser.add_argument('--musictag-config')
    plan_parser=sub.add_parser('plan',aliases=['tag'],help='plan 只预览；tag 自动匹配并写入（先备份）')
    plan_parser.add_argument('path'); plan_parser.add_argument('-r','--recursive',action='store_true')
    plan_parser.add_argument('--out',help='可选：保存预览/执行报告；默认不保存，仅控制台显示')
    plan_parser.add_argument('--musictag-config')
    plan_parser.add_argument('--sources',help='netease,qq（顺序即优先级）')
    plan_parser.add_argument('--workers',type=int,choices=range(1,6),default=None)
    plan_parser.add_argument('--id',help='单文件精确歌曲 ID，默认网易云数字 ID')
    plan_parser.add_argument('--source',choices=['netease','qq'],help='--id 对应平台')
    plan_parser.add_argument('--ncm-root',help='读取同名 NCM 元数据中的歌曲 ID，不转换音频')
    plan_parser.add_argument('--cache',default=str(ROOT/'cache'))
    plan_parser.add_argument('--keep-backups',action='store_true',help='保留成功文件的原始备份，默认批次结束后清理')
    apply_parser=sub.add_parser('apply',help='执行预览计划，先备份再写入，不再联网')
    apply_parser.add_argument('plan')
    apply_parser.add_argument('--out',help='可选：保存执行结果；默认只在控制台显示')
    apply_parser.add_argument('--keep-backups',action='store_true',help='保留成功文件的原始备份')
    args=parser.parse_args()
    if args.command=='inspect':
        print(json.dumps(read_tags(Path(args.path)),ensure_ascii=False,indent=2)); return 0
    if args.command=='settings':
        print(json.dumps(source_settings(args.musictag_config),ensure_ascii=False,indent=2)); return 0
    if args.command=='apply':
        return apply_plan(args.plan,keep_backups=args.keep_backups,report=args.out)
    settings=source_settings(args.musictag_config)
    if args.sources:
        settings['sources']=[s.strip() for s in args.sources.split(',')]
        for name in ('LyricInfo_SourceItemList','PictureInfo_SourceItemList'):
            settings[name]=settings['sources']
    all_sources=set(settings['sources']+settings.get('LyricInfo_SourceItemList',[])+settings.get('PictureInfo_SourceItemList',[]))
    unsupported=all_sources-{'netease','qq'}
    if unsupported or not settings['sources']:
        parser.error('本版支持你当前启用的网易云、QQ 源；未实现的源不会静默替换: '+','.join(unsupported))
    if args.workers:
        settings['workers']=args.workers
    files=files_in(args.path,args.recursive)
    if not files:
        parser.error('没有找到 MP3/FLAC 文件')
    if args.id and len(files)!=1:
        parser.error('--id 仅支持一个文件')
    ncm_lookup={}
    if args.ncm_root:
        root=Path(args.ncm_root)
        if not root.is_dir():
            parser.error('NCM 目录不存在')
        for p in root.rglob('*.ncm'):
            ncm_lookup.setdefault(p.stem,[]).append(p)
    out=Path(args.out).resolve() if args.out else None
    if out is not None and (out.exists() or out.with_name(out.stem+'.assets').exists()):
        parser.error('预览或资源目录已存在，请使用新的 --out 文件名')

    def process(assets):
        client=Client(args.cache)
        start=time.monotonic(); entries=[]
        with concurrent.futures.ThreadPoolExecutor(max_workers=settings['workers']) as pool:
            pending={pool.submit(make_entry,p,args,settings,client,ncm_lookup,assets):p for p in files}
            for future in concurrent.futures.as_completed(pending):
                entry=future.result(); entries.append(entry)
                status_print(f'[{len(entries)}/{len(files)}] {entry["status"]}: {Path(entry["path"]).name}',
                             'error' if entry['status']=='error' else None)
                if entry.get('reason'):
                    status_print('  '+entry['reason'],'error' if entry['status']=='error' else None)
                for warning in entry.get('warnings',[]):
                    print('  '+warning,flush=True)
        entries.sort(key=lambda e:e['path'])
        seconds=round(time.monotonic()-start,2)
        plan={'schema':1,'settings':settings,'protected':list(PROTECTED),'seconds':seconds,'entries':entries}
        if out is not None:
            json_write(out,plan)
            print('预览: '+str(out))
        print(f'匹配耗时: {seconds} 秒。')
        if args.command=='tag':
            report=out.with_name(out.stem+'.result.json') if out is not None else None
            return apply_plan(plan=plan,assets=assets,keep_backups=args.keep_backups,report=report)
        for entry in entries:
            if entry.get('changes'):
                print(Path(entry['path']).name+'：拟更新 '+', '.join(entry['changes']))
        print('仅预览，未修改音频。')
        if out is not None:
            print('使用 apply 执行。')
        else:
            print('默认不保存预览；需要稍后 apply 时请指定 --out。')
        if any(e['status']=='error' for e in entries):
            return 1
        return 2 if any(e['status']=='review' for e in entries) else 0

    if out is not None:
        assets=out.with_name(out.stem+'.assets'); assets.mkdir(parents=True)
        return process(assets)
    # Assets live only for this invocation. No plan/result files or reports folder by default.
    with tempfile.TemporaryDirectory(prefix='music-tag-assets-') as temp:
        return process(Path(temp))

if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8',errors='replace')
        sys.stderr.reconfigure(encoding='utf-8',errors='replace')
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        status_print('错误: '+str(e),'error',file=sys.stderr); sys.exit(1)
