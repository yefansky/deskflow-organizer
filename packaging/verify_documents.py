"""Validate/stage only the reviewed, self-contained agent documentation.

Also used against the real installed directory by disposable CI acceptance.
"""
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import unquote

SKILL_FILES = ('SKILL.md', 'README.md', 'references/connection.md', 'references/api.md', 'references/troubleshooting.md')
ROOT_FILES = ('README.md', 'AI接入说明.md')


def verify_documents(root):
    root = Path(root).resolve()
    skill = root / 'skills/deskflow-organizer'
    actual = {p.relative_to(skill).as_posix() for p in skill.rglob('*') if p.is_file()}
    if actual != set(SKILL_FILES):
        raise ValueError(f'Unreviewed or missing skill files: {actual ^ set(SKILL_FILES)}')
    for rel in (*ROOT_FILES, *(f'skills/deskflow-organizer/{f}' for f in SKILL_FILES)):
        path = root / rel
        content = path.read_text(encoding='utf-8-sig')
        # Refuse known private source footprints, personal paths and credential formats.
        for pattern in (r'(?i)[A-Z]:[\\/]Users[\\/](?!你的用户名)[^\\/\s`]+',
                        r'(?i)[FG]:[\\/]private_brain', r'yefan1|叶帆|私人文件夹|实验室[\\/]',
                        r'(?i)(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,})',
                        r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',
                        r'(?i)\b(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*[\x22\x27]?[A-Za-z0-9_/-]{12,}'):
            if re.search(pattern, content):
                raise ValueError(f'Potential private content in {rel}; review before distribution')
        prose = re.sub(r'```.*?```', '', content, flags=re.S)
        for link in re.findall(r'\]\(([^)]+)\)', prose):
            if '://' in link or link.startswith('#'):
                continue
            target = (path.parent / unquote(link.split('#')[0])).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise ValueError(f'Broken/out-of-package documentation link: {rel}: {link}')
    header = (skill / 'SKILL.md').read_text(encoding='utf-8')
    if not header.startswith('---\nname: deskflow-organizer\ndescription:'):
        raise ValueError('Invalid skill frontmatter')
    print('Reviewed agent documentation: complete, local links valid, privacy patterns clear.')


def stage_documents(source, destination):
    source, destination = Path(source), Path(destination)
    verify_documents(source)
    for rel in (*ROOT_FILES, *(f'skills/deskflow-organizer/{f}' for f in SKILL_FILES)):
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, target)
    verify_documents(destination)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--stage':
        stage_documents(sys.argv[2], sys.argv[3])
    else:
        verify_documents(sys.argv[1])
