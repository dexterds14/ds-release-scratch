#!/usr/bin/env python3
"""Parse GitHub workflow files strictly: valid YAML, no duplicate mapping keys, no conflict markers.

    check_yaml.py [FILE ...]      (default: .github/workflows/*.yml and *.yaml under the repo root)

Why it exists (ENG-2233): nothing else parses the release workflows before an RC build runs them.
`./run-kas lint` loads only build.yml and dsensos_build.yml and reads release-*.yml as text, so
conflict markers left in release-promote.yml stay green in pr-checks. A merge that keeps both
sides of an adjacent edit (git merge-file --union on the 2026-11-02 vertex/1.x.x conflict) gives
two `runner:` keys in one job; PyYAML's safe_load keeps the last one without a word, so a plain
parse passes it too. This loader refuses a mapping that names a key twice.

Exit 0 when every file passes, 1 when any fails (each problem printed as file:line: message),
2 when PyYAML is missing. Needs PyYAML (pr-checks installs it the way the kas lint step does).
"""
import glob
import os
import re
import sys

MARKER = re.compile(r'^(<<<<<<< |\|\|\|\|\|\|\| |=======$|>>>>>>> )')

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the runner
    print('check_yaml.py: PyYAML is not installed (python3 -m pip install pyyaml)', file=sys.stderr)
    sys.exit(2)


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that raises on a mapping key that appears twice in one mapping."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = {}
            for key_node, _ in node.value:
                if key_node.tag == 'tag:yaml.org,2002:merge':
                    continue  # `<<:` merge keys are resolved by flatten_mapping in the base class
                key = self.construct_object(key_node, deep=True)
                try:
                    hash(key)
                except TypeError:
                    continue  # unhashable keys fail in the base class with its own message
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None,
                        f'duplicate key {key!r} (first at line {seen[key] + 1})',
                        key_node.start_mark)
                seen[key] = key_node.start_mark.line
        return super().construct_mapping(node, deep=deep)


def check(path):
    """[(line, message)] for one file; empty when it passes."""
    problems = []
    with open(path, encoding='utf-8') as f:
        text = f.read()
    for n, line in enumerate(text.splitlines(), 1):
        if MARKER.match(line):
            problems.append((n, f'conflict marker {line.split()[0] if line.split() else line!r}'))
    if problems:
        return problems  # the parse error a marker causes adds nothing
    try:
        for _ in yaml.load_all(text, Loader=UniqueKeyLoader):
            pass
    except yaml.YAMLError as e:
        mark = getattr(e, 'problem_mark', None)
        line = mark.line + 1 if mark is not None else 0
        msg = getattr(e, 'problem', None) or str(e).splitlines()[0]
        problems.append((line, msg))
    return problems


def default_files(root):
    found = []
    for pattern in ('*.yml', '*.yaml'):
        found += glob.glob(os.path.join(root, '.github', 'workflows', pattern))
    return sorted(found)


def main(argv):
    files = argv or default_files(os.getcwd())
    if not files:
        print('check_yaml.py: no workflow files found', file=sys.stderr)
        return 1
    bad = 0
    for path in files:
        problems = check(path)
        for line, msg in problems:
            print(f'::error file={path},line={line}::{path}:{line}: {msg}')
        bad += bool(problems)
    print(f'check_yaml.py: {len(files)} file(s), {bad} with problems')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
