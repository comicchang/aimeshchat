#!/usr/bin/env python3
"""Oracle session migration: unify layout to _oracle/<safe-key>/.

Scans _oracle/<project>/ for historical sessions, matches them to
ParkRegistry entries by backend_session_id, and moves to _oracle/<safe-key>/
layout. Sessions without registry entries get a fallback name based on
their original project directory + session ID prefix.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
from pathlib import Path


def _safe_key(review_key: str) -> str:
    """Convert review_key to filesystem-safe directory name."""
    return review_key.replace(':', '-').replace('/', '-')[:64]


def _fallback_name(project: str, session_id: str) -> str:
    """Generate fallback name for sessions without registry entry."""
    # Use project + short session ID
    short_sid = session_id[:12] if session_id else 'unknown'
    safe_project = project.lstrip('-').replace('/', '-')[:32]
    return f"legacy-{safe_project}-{short_sid}"


def migrate(
    sessions_root: Path,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """Execute migration. Returns report dict."""
    oracle_root = sessions_root / '_oracle'
    if not oracle_root.is_dir():
        return {'error': '_oracle directory not found', 'moved': 0}

    # Load registry for review_key → backend_session_id mapping
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
        from codeagent.park.registry import ParkRegistry
        reg = ParkRegistry()
        sid_to_key: dict[str, str] = {}
        for m in reg.list_active():
            if m.backend_session_id:
                sid_to_key[m.backend_session_id] = m.review_key
    except Exception as e:
        print(f"Warning: could not load registry: {e}", file=sys.stderr)
        sid_to_key = {}

    report = {
        'moved': 0,
        'skipped': 0,
        'unmatched': 0,
        'errors': [],
        'layout': 'safe-key',
    }

    # Scan _oracle/<project>/ directories
    for project_dir in sorted(oracle_root.iterdir()):
        if not project_dir.is_dir():
            continue
        # Skip directories that already look like safe-key layout
        # (they contain .jsonl files directly, not nested under project dirs)
        jsonl_files = list(project_dir.glob('*.jsonl'))
        if not jsonl_files:
            continue

        for f in jsonl_files:
            fname = f.stem
            # Extract session ID from filename: <timestamp>_<sessionId>
            parts = fname.split('_')
            sid = parts[-1] if len(parts) > 1 else ''

            if sid in sid_to_key:
                # Known session → move to safe-key layout
                review_key = sid_to_key[sid]
                target_dir = oracle_root / _safe_key(review_key)
            else:
                # Unknown session → use fallback name
                target_dir = oracle_root / _fallback_name(project_dir.name, sid)

            target_file = target_dir / f.name

            if target_file.exists():
                report['skipped'] += 1
                if verbose:
                    print(f"  SKIP (exists): {f.name}")
                continue

            if sid not in sid_to_key:
                report['unmatched'] += 1

            if dry_run:
                report['moved'] += 1
                if verbose:
                    print(f"  DRY-RUN: {project_dir.name}/{f.name} → {target_dir.name}/")
                continue

            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(target_file))
                report['moved'] += 1

                # Move associated directory (session metadata)
                assoc = project_dir / f.stem
                if assoc.is_dir():
                    t_assoc = target_dir / assoc.name
                    if not t_assoc.exists():
                        shutil.move(str(assoc), str(t_assoc))

                if verbose:
                    print(f"  MOVED: {project_dir.name}/{f.name} → {target_dir.name}/")
            except Exception as e:
                report['errors'].append(f"{f.name}: {e}")

    # Clean up empty project directories
    if not dry_run:
        for d in list(oracle_root.iterdir()):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                if verbose:
                    print(f"  RMDIR: {d.name}")

    # Update registry manifests
    if not dry_run and sid_to_key:
        try:
            import dataclasses as dc
            updated = 0
            for m in reg.list_active():
                if not m.backend_session_id:
                    continue
                safe = _safe_key(m.review_key)
                target = oracle_root / safe
                if target.exists():
                    new_m = dc.replace(m, omp_session_dir=str(target))
                    reg.update(m.review_key, new_m)
                    updated += 1
            report['registry_updated'] = updated
        except Exception as e:
            report['errors'].append(f"registry update: {e}")

    return report


def main():
    parser = argparse.ArgumentParser(description='Migrate oracle sessions to unified layout')
    parser.add_argument('--dry-run', action='store_true', help='Only report, do not move files')
    parser.add_argument('--verbose', '-v', action='store_true', help='Print each move')
    parser.add_argument('--sessions-root', type=Path,
                        default=Path.home() / '.omp' / 'agent' / 'sessions',
                        help='Session root directory')
    args = parser.parse_args()

    report = migrate(args.sessions_root, dry_run=args.dry_run, verbose=args.verbose)

    print(f"\nMigration {'(dry-run) ' if args.dry_run else ''}complete:")
    print(f"  Moved: {report['moved']}")
    print(f"  Skipped: {report['skipped']}")
    print(f"  Unmatched (no registry): {report['unmatched']}")
    if report.get('registry_updated'):
        print(f"  Registry updated: {report['registry_updated']}")
    if report['errors']:
        print(f"  Errors: {len(report['errors'])}")
        for e in report['errors']:
            print(f"    - {e}")

    return 0 if not report['errors'] else 1


if __name__ == '__main__':
    sys.exit(main())
