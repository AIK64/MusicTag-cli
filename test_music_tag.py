"""Run with Python 3.10+ and ffmpeg; tests use generated audio in a temp directory."""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import music_tag as m

FFMPEG = shutil.which('ffmpeg')

def audio_hash(path):
    return subprocess.check_output([FFMPEG,'-v','error','-i',str(path),'-map','0:a:0',
                                    '-c:a','copy','-f','hash','-'],text=True).strip()

class MatchingTests(unittest.TestCase):
    def test_ambiguous_versions_not_automatically_selected(self):
        local=dict(title=['Song'],artist=['Artist'],album=[],duration=200)
        candidates=[dict(id='1',title='Song',artist=['Artist'],album='Album A',duration=200),
                    dict(id='2',title='Song',artist=['Artist'],album='Album B',duration=200)]
        self.assertIsNone(m.choose(candidates,local))
        local['album']=['Album B']
        self.assertEqual(m.choose(candidates,local)['id'],'2')

    def test_wrong_live_version_artist_or_duration_rejected(self):
        local=dict(title=['Song'],artist=['Artist'],album=['Album'],duration=200)
        song=dict(id='1',title='Song (Live)',artist=['Artist'],album='Album',duration=200)
        self.assertIsNone(m.choose([song],local))
        song.update(title='Song',artist=['Cover Artist'])
        self.assertIsNone(m.choose([song],local))
        song.update(artist=['Artist'],duration=210)
        self.assertIsNone(m.choose([song],local))

    def test_translations_keep_synchronized_timestamps(self):
        value=m.translated_lyrics('[00:01.00]Hello\n[00:02.00]World','[00:01.000]你好\n[00:02.000]世界')
        self.assertEqual(value,'[00:01.00]Hello\n[00:01.00]你好\n[00:02.00]World\n[00:02.00]世界')

    def test_ncm_metadata_only_id(self):
        from Crypto.Cipher import AES
        text=b'music:'+json.dumps({'musicId':123456}).encode()
        padding=16-len(text)%16
        encrypted=AES.new(b'#14ljk_!\\]&0U<\'(',AES.MODE_ECB).encrypt(text+bytes([padding])*padding)
        encoded=b"163 key(Don't modify):"+base64.b64encode(encrypted)
        # NCM's metadata base64 starts after its 22-byte prefix.
        prefix=b"163 key(Don't modify): "
        meta=bytes(x^0x63 for x in prefix+base64.b64encode(encrypted))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'test.ncm'
            p.write_bytes(b'CTENFDAM'+b'\0\0'+struct.pack('<I',0)+struct.pack('<I',len(meta))+meta)
            self.assertEqual(m.ncm_id(p),'123456')
        self.assertEqual(m.from_163key({'comment':encoded.decode()}),'123456')

@unittest.skipUnless(FFMPEG,'ffmpeg not on PATH')
class FileTests(unittest.TestCase):
    def setUp(self):
        work=Path.cwd()/'work'/'music-tag-tests'
        work.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=work)
        self.root=Path(self.temp.name)
        self.assets=self.root/'plan.assets'; self.assets.mkdir()
        self.cover=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF10AAAAASUVORK5CYII=')
        (self.assets/'cover.png').write_bytes(self.cover)

    def tearDown(self):
        self.temp.cleanup()

    def make_audio(self,ext):
        path=self.root/('test'+ext)
        subprocess.run([FFMPEG,'-v','error','-f','lavfi','-i','sine=frequency=440:duration=1',
                        '-metadata','title=Original 标题','-metadata','artist=Original 艺术家',
                        '-metadata','album=Original 专辑','-metadata','comment=Keep this comment',str(path)],check=True)
        return path

    def make_plan(self,path):
        changes={'year':['2024'],'track':['7'],'disc':['2'],'genre':['Rock'],
                 'albumartist':['Album Artist'],'composer':['Composer'],'lyricist':['Lyricist'],
                 'lyrics':'[00:00.00]内嵌歌词\n[00:00.00]Embedded lyrics',
                 'cover':dict(asset='cover.png',sha256=hashlib.sha256(self.cover).hexdigest(),
                              mime='image/png',width=1,height=1),
                 'NETEASE_SONG_ID':'123456','QQ_SONG_MID':'qqmid'}
        entry=dict(path=str(path),status='ready',before=m.read_tags(path),sha256=m.digest(path),changes=changes)
        plan=dict(schema=1,settings={'id3_version':3},entries=[entry])
        m.json_write(self.root/'plan.json',plan)
        return plan

    def test_mp3_and_flac_embed_all_fields_preserve_protected_and_audio(self):
        for ext in ('.mp3','.flac'):
            with self.subTest(ext=ext):
                p=self.make_audio(ext); plan=self.make_plan(p)
                original=p.read_bytes(); before=m.read_tags(p); checksum=audio_hash(p)
                self.assertEqual(m.apply_plan(self.root/'plan.json',keep_backups=True),0)
                after=m.read_tags(p)
                m.validate_changes(before,after,plan['entries'][0]['changes'])
                self.assertEqual(checksum,audio_hash(p))
                self.assertEqual(after['comment'],before['comment'])
                self.assertEqual(list(p.parent.glob('*.lrc')),[])
                self.assertEqual(list(p.parent.glob('*.png')),[])
                backup=next((p.parent/'.music-tag-backups').glob(p.name+'*.bak'))
                self.assertEqual(backup.read_bytes(),original)

    def test_changed_file_is_not_overwritten(self):
        p=self.make_audio('.mp3'); self.make_plan(p)
        p.write_bytes(p.read_bytes()+b'external edit')
        changed=p.read_bytes()
        self.assertEqual(m.apply_plan(self.root/'plan.json'),1)
        self.assertEqual(p.read_bytes(),changed)

    def test_missing_or_changed_cover_does_not_touch_original(self):
        p=self.make_audio('.flac'); self.make_plan(p)
        original=p.read_bytes(); (self.assets/'cover.png').write_bytes(b'not an image')
        self.assertEqual(m.apply_plan(self.root/'plan.json'),1)
        self.assertEqual(p.read_bytes(),original)

    def test_plan_cannot_modify_protected_fields(self):
        p=self.make_audio('.mp3'); plan=self.make_plan(p)
        original=p.read_bytes(); plan['entries'][0]['changes']['title']=['Wrong Title']
        m.json_write(self.root/'plan.json',plan)
        self.assertEqual(m.apply_plan(self.root/'plan.json'),1)
        self.assertEqual(p.read_bytes(),original)

    def test_empty_metadata_preserves_original_and_instrumental_skips_lyrics(self):
        p=self.make_audio('.flac')
        a=m.FLAC(p); a['title']=['Song Instrumental']; a['artist']=['Artist']; a['album']=['Album']; a['lyrics']=['Original lyrics']; a['genre']=['Original genre']; a.save()
        args=argparse.Namespace(id=None,source=None)
        settings=m.source_settings(); settings['sources']=['netease']
        song=dict(source='netease',id='1',title='Song Instrumental',artist=['Artist'],album='Album',duration=1)
        from unittest.mock import patch
        with patch.object(m,'search',return_value=[song]),patch.object(m,'detail',return_value=song),patch.object(m,'lyrics',side_effect=AssertionError('must not fetch')):
            entry=m.make_entry(p,args,settings,None,{},self.assets)
        self.assertNotIn('lyrics',entry['changes'])
        self.assertNotIn('genre',entry['changes'])
        self.assertTrue(any('伴奏' in x for x in entry['warnings']))

    def test_missing_lyrics_or_translation_does_not_block_other_fields(self):
        from unittest.mock import patch
        for ext, remote_lyrics in (('.mp3', ''), ('.flac', '[00:00.00]Original without translation')):
            with self.subTest(ext=ext):
                p=self.make_audio(ext)
                m.put_tags(p, {'lyrics':'Existing lyrics', 'year':['1990'], 'genre':['Old genre']}, self.assets, 3)
                before=m.read_tags(p)
                args=argparse.Namespace(id=None,source=None)
                settings=m.source_settings(); settings['sources']=['netease']
                song=dict(source='netease',id='1',title=before['title'][0],artist=before['artist'],
                          album=before['album'][0],duration=before['duration'],year='2025',genre=['New genre'],
                          cover_url='https://example.invalid/test.png')
                cover=self.cover
                class Client:
                    def request(self,*args,**kwargs):
                        return cover
                with patch.object(m,'search',return_value=[song]),patch.object(m,'detail',return_value=song),patch.object(m,'lyrics',return_value=remote_lyrics):
                    entry=m.make_entry(p,args,settings,Client(),{},self.assets)
                self.assertEqual(entry['status'],'ready')
                self.assertIn('year',entry['changes'])
                self.assertIn('genre',entry['changes'])
                self.assertIn('cover',entry['changes'])
                if not remote_lyrics:
                    self.assertNotIn('lyrics',entry['changes'])
                plan=dict(schema=1,settings={'id3_version':3},entries=[entry])
                m.json_write(self.root/'plan.json',plan)
                self.assertEqual(m.apply_plan(self.root/'plan.json'),0)
                after=m.read_tags(p)
                self.assertEqual(after['year'],['2025'])
                self.assertEqual(after['genre'],['New genre'])
                self.assertTrue(after['cover'])
                actual=after['lyrics']
                actual='\n'.join(actual) if isinstance(actual,list) else actual
                self.assertEqual(actual,remote_lyrics or 'Existing lyrics')
                for field in m.PROTECTED:
                    self.assertEqual(after[field],before[field])

    def test_successful_batch_cleans_new_backups_and_temporary_files(self):
        p=self.make_audio('.mp3'); self.make_plan(p)
        self.assertEqual(m.apply_plan(self.root/'plan.json',report=self.root/'plan.result.json'),0)
        self.assertFalse((self.root/'.music-tag-backups').exists())
        self.assertEqual([p for p in self.root.glob('.music-tag-*') if p.is_file()],[])
        report=json.loads((self.root/'plan.result.json').read_text(encoding='utf-8'))
        self.assertTrue(report[0]['backup_cleaned'])
        self.assertFalse(report[0]['backup_retained'])

    def test_cleanup_does_not_remove_historical_backups(self):
        p=self.make_audio('.flac'); plan=self.make_plan(p)
        folder=self.root/'.music-tag-backups'; folder.mkdir()
        history=folder/'historical.bak'; history.write_bytes(b'Historical data')
        matching=folder/(p.name+'.'+plan['entries'][0]['sha256'][:16]+'.bak')
        original=p.read_bytes(); matching.write_bytes(original)
        self.assertEqual(m.apply_plan(self.root/'plan.json'),0)
        self.assertEqual(history.read_bytes(),b'Historical data')
        self.assertEqual(matching.read_bytes(),original)

    def test_failed_replace_keeps_backup_but_removes_temp(self):
        from unittest.mock import patch
        p=self.make_audio('.mp3'); self.make_plan(p)
        original=p.read_bytes()
        real_replace=m.os.replace
        def replace(src,dst):
            if Path(dst)==p:
                raise PermissionError('Simulated file in use')
            return real_replace(src,dst)
        with patch.object(m.os,'replace',side_effect=replace):
            self.assertEqual(m.apply_plan(self.root/'plan.json'),1)
        self.assertEqual(p.read_bytes(),original)
        backup=next((self.root/'.music-tag-backups').glob('*.bak'))
        self.assertEqual(backup.read_bytes(),original)
        self.assertEqual([p for p in self.root.glob('.music-tag-*') if p.is_file()],[])

    def test_apply_does_not_save_report_by_default(self):
        p=self.make_audio('.mp3'); self.make_plan(p)
        self.assertEqual(m.apply_plan(self.root/'plan.json'),0)
        self.assertFalse((self.root/'plan.result.json').exists())

    def test_default_tag_keeps_plan_in_memory_and_removes_temp_assets(self):
        from unittest.mock import patch
        p=self.make_audio('.flac'); plan=self.make_plan(p)
        tool=self.root/'tool'; tool.mkdir()
        temporary=[]
        real_temp=m.tempfile.TemporaryDirectory
        def tempdir(**kwargs):
            obj=real_temp(dir=self.root,**kwargs); temporary.append(Path(obj.name)); return obj
        def entry(path,args,settings,client,ncm_lookup,assets):
            (assets/'cover.png').write_bytes(self.cover)
            return copy.deepcopy(plan['entries'][0])
        with patch.object(m,'ROOT',tool),patch.object(m,'Client'),patch.object(m,'make_entry',side_effect=entry),patch.object(m.tempfile,'TemporaryDirectory',side_effect=tempdir),patch.object(m.sys,'argv',['music_tag.py','tag',str(p)]):
            self.assertEqual(m.main(),0)
        self.assertTrue(m.read_tags(p)['lyrics'])
        self.assertFalse((tool/'reports').exists())
        self.assertFalse(list(tool.glob('*.json')))
        self.assertTrue(temporary)
        self.assertTrue(all(not t.exists() for t in temporary))

    def test_explicit_out_saves_plan_and_execution_report(self):
        from unittest.mock import patch
        p=self.make_audio('.mp3'); plan=self.make_plan(p)
        def entry(path,args,settings,client,ncm_lookup,assets):
            (assets/'cover.png').write_bytes(self.cover)
            return copy.deepcopy(plan['entries'][0])
        out=self.root/'explicit.json'
        with patch.object(m,'Client'),patch.object(m,'make_entry',side_effect=entry),patch.object(m.sys,'argv',['music_tag.py','tag',str(p),'--out',str(out)]):
            self.assertEqual(m.main(),0)
        self.assertTrue(out.is_file())
        self.assertTrue((self.root/'explicit.result.json').is_file())
        self.assertTrue((self.root/'explicit.assets'/'cover.png').is_file())

if __name__=='__main__':
    unittest.main(verbosity=2)
