"""
Parses Arknights story .txt files into structured scene objects.

Format example:
  [HEADER(title="...")]
  [Background(image="...")]
  [name="도베르만"] 대사 내용
  plain narration text
"""

import re
from typing import Optional

CMD_RE = re.compile(r'^\[(\w+)\((.*)\)\]$', re.DOTALL)
NAME_RE = re.compile(r'^\[name="([^"]+)"\]\s*(.*)')
INLINE_CMD_RE = re.compile(r'\[/?[A-Za-z][^\]]*\]')


def _parse_args(raw: str) -> dict:
    result = {}
    for m in re.finditer(r'(\w+)="([^"]*)"', raw):
        result[m.group(1)] = m.group(2)
    return result


def _strip_inline_cmds(text: str) -> str:
    return INLINE_CMD_RE.sub('', text).strip()


def parse_story(raw_text: str) -> list[dict]:
    """
    Returns list of scene dicts:
      { type: 'dialogue'|'narration'|'decision', speaker, text, background, cmd }
    """
    scenes = []
    current_bg = None
    current_name = None
    pending_decision = None

    lines = raw_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1

        if not line:
            continue

        # Named dialogue: [name="X"] text  OR  [name="X"] on its own line
        m = NAME_RE.match(line)
        if m:
            speaker = m.group(1)
            text = _strip_inline_cmds(m.group(2))
            if text:
                scenes.append({
                    'type': 'dialogue',
                    'speaker': speaker,
                    'text': text,
                    'background': current_bg,
                })
                current_name = None  # text already captured; next line is narration
            else:
                current_name = speaker  # next plain line is this speaker's dialogue
            continue

        # Command line
        if line.startswith('['):
            # Handle multi-line commands (rare)
            full = line
            while not full.endswith(']') and i < len(lines):
                full += ' ' + lines[i].strip()
                i += 1

            # [name="X"] can appear without following text (next line is the text)
            m2 = NAME_RE.match(full)
            if m2:
                current_name = m2.group(1)
                text = _strip_inline_cmds(m2.group(2))
                if text:
                    scenes.append({
                        'type': 'dialogue',
                        'speaker': current_name,
                        'text': text,
                        'background': current_bg,
                    })
                continue

            cmd_m = CMD_RE.match(full)
            if cmd_m:
                cmd = cmd_m.group(1).lower()
                args = _parse_args(cmd_m.group(2))
                if cmd == 'background':
                    current_bg = args.get('image') or args.get('name')
                elif cmd == 'decision':
                    # Collect options
                    options = re.findall(r'options="([^"]+)"', cmd_m.group(2))
                    if options:
                        scenes.append({
                            'type': 'decision',
                            'speaker': None,
                            'text': '선택지: ' + ' / '.join(options),
                            'background': current_bg,
                        })
                elif cmd == 'predicate':
                    pass  # skip game logic
            continue

        # Plain text (narration, or dialogue continuation after [name=...])
        text = _strip_inline_cmds(line)
        if not text:
            continue

        if current_name:
            scenes.append({
                'type': 'dialogue',
                'speaker': current_name,
                'text': text,
                'background': current_bg,
            })
            current_name = None  # reset: next plain line is narration
        else:
            scenes.append({
                'type': 'narration',
                'speaker': None,
                'text': text,
                'background': current_bg,
            })

    return scenes


def extract_title(raw_text: str, path: str) -> str:
    m = re.search(r'\[HEADER\([^)]*title="([^"]+)"', raw_text)
    if m:
        return m.group(1)
    # Derive from path
    return path.split('/')[-1].replace('.txt', '')
