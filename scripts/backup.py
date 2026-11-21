# -*- coding: utf-8 -*-
"""
scripts/backup.py — نسخ احتياطي مضغوط لبيانات كل المستأجرين (معرفة + تذاكر + سياسات + سجلات).

التشغيل:    python scripts/backup.py
الإعدادات (اختيارية في .env):
    BACKUP_KEEP=14      عدد النسخ التي تُحتفظ بها (الأقدم يحذف تلقائياً)

النسخ تُحفظ في backups/backup_YYYYMMDD_HHMMSS.zip — آمن للجدولة:
Windows Task Scheduler يومياً، أو Linux cron.
"""
import os
import sys
import time
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')

# ما يُنسخ: بيانات المستأجرين فقط (الموديلات والكود مستثناة)
INCLUDE_DIRS = [os.path.join(BASE_DIR, 'data', 'tenants')]


def load_backup_keep():
    keep = os.environ.get('BACKUP_KEEP', '14')
    try:
        return max(1, int(keep))
    except ValueError:
        return 14


def load_env():
    env_path = os.path.join(BASE_DIR, '.env')
    if os.path.isfile(env_path):
        with open(env_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('\'"'))


def checkpoint_sqlite():
    """دمج ملفات WAL في قواعد البيانات قبل النسخ — بدونه قد تضيع آخر التذاكر
    من النسخة الاحتياطية لأن SQLite بوضع WAL يحتفظ بالكتابات الحديثة في -wal."""
    import sqlite3
    checked = 0
    for root_dir in INCLUDE_DIRS:
        for root, _, names in os.walk(root_dir):
            for name in names:
                if name.endswith('.db'):
                    db_path = os.path.join(root, name)
                    try:
                        conn = sqlite3.connect(db_path, timeout=5)
                        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                        conn.close()
                        checked += 1
                    except (sqlite3.Error, OSError) as e:
                        print('  ! تعذر checkpoint لـ %s: %s (سيتم أرشفة الـ WAL مع النسخة)' % (name, e), file=sys.stderr)
    return checked


def make_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    zip_path = os.path.join(BACKUP_DIR, 'backup_%s.zip' % stamp)
    files = 0
    checkpoint_sqlite()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root_dir in INCLUDE_DIRS:
            if not os.path.isdir(root_dir):
                continue
            for root, _, names in os.walk(root_dir):
                for name in names:
                    if name.endswith('.tmp'):
                        continue
                    full = os.path.join(root, name)
                    arcname = os.path.relpath(full, BASE_DIR)
                    try:
                        zf.write(full, arcname)
                        files += 1
                    except OSError as e:
                        print('  ! تخطي %s: %s' % (arcname, e), file=sys.stderr)
    return zip_path, files


def prune_old(keep):
    backups = sorted(f for f in os.listdir(BACKUP_DIR)
                     if f.startswith('backup_') and f.endswith('.zip'))
    removed = 0
    for old in backups[:-keep] if len(backups) > keep else []:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
            removed += 1
        except OSError:
            pass
    return removed


def main():
    load_env()
    zip_path, files = make_backup()
    keep = load_backup_keep()
    removed = prune_old(keep)
    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print('✅ نسخة احتياطية: %s (%d ملف، %.1f MB) — الاحتفاظ بآخر %d نسخ%s' %
          (os.path.basename(zip_path), files, size_mb, keep,
           ' (حذفت %d قديمة)' % removed if removed else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
