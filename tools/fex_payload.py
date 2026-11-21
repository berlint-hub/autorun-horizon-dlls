"""Build and validate the pinned Horizon FEX payload."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess

VERSION = '2611.1'
REVISION = '4f9c1f1e36443d22d7655477e34f51b95b685bf1'
# Autorun's port of its Horizon patch to that release (horizon-wine/fex).
PATCH = 'horizon-2611.patch'
DLLS = {'libarm64ecfex.dll': 'arm64ec', 'libwow64fex.dll': 'arm64'}
# The runtime side of the FEX ABI stays with the runtime, in Autorun.
from autorun import horizon_wine as HORIZON_WINE


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_digest(path):
    return hashlib.sha256(Path(path).read_text().encode()).hexdigest()


# Horizon does not let a program read the virtual counter or touch the virtual
# timer (CNTKCTL_EL1 keeps EL0VCTEN and EL0VTEN off): such an instruction
# traps as an illegal one. The physical counter, CNTPCT_EL0, is readable.
# FEX-2611's RDRAND fallback for Windows seeded itself with
# __builtin_readcyclecounter(), CNTVCT_EL0, when the module was loaded.
# MRS and MSR of op0 3, op1 3, CRn 14: CRm 0 with op2 2 (CNTVCT_EL0) and
# 6 (CNTVCTSS_EL0), and CRm 3 (CNTV_TVAL_EL0, CNTV_CTL_EL0, CNTV_CVAL_EL0).
VIRTUAL_COUNTER_ACCESS = (
    (0xFFDFFFE0, 0xD51BE040, 'CNTVCT_EL0'),
    (0xFFDFFFE0, 0xD51BE0C0, 'CNTVCTSS_EL0'),
    (0xFFDFFF00, 0xD51BE300, 'CNTV_*_EL0'),
)


def virtual_counter_accesses(data):
    """Offsets of the instructions in a PE image's executable sections that
    Horizon traps: accesses to the virtual counter and timer."""
    offset = struct.unpack_from('<I', data, 0x3c)[0]
    sections = offset + 24 + struct.unpack_from('<H', data, offset + 20)[0]
    found = []
    for section in range(struct.unpack_from('<H', data, offset + 6)[0]):
        name, size, rva, raw_size, raw = struct.unpack_from('<8sIIII', data, sections + section * 40)
        if not struct.unpack_from('<I', data, sections + section * 40 + 36)[0] & 0x20000000:
            continue
        end = min(raw + min(size, raw_size), len(data))
        for at in range(raw, end - 3, 4):
            word = struct.unpack_from('<I', data, at)[0]
            for mask, value, register in VIRTUAL_COUNTER_ACCESS:
                if word & mask == value:
                    found.append((rva + at - raw, register))
    return found


def validate_image(path, architecture):
    data = Path(path).read_bytes()
    if len(data) < 64 or data[:2] != b'MZ':
        raise ValueError('Invalid FEX PE image')
    offset = struct.unpack_from('<I', data, 0x3c)[0]
    if (offset > len(data) - 26 or data[offset:offset + 4] != b'PE\0\0' or
            struct.unpack_from('<H', data, offset + 4)[0] not in (
                (0x8664, 0xa641) if architecture == 'arm64ec' else (0xaa64,)) or
            struct.unpack_from('<H', data, offset + 24)[0] != 0x20b or
            not struct.unpack_from('<H', data, offset + 22)[0] & 0x2000):
        raise ValueError(f'{path} is not {architecture} PE32+')
    if offset + 24 + 240 > len(data):
        raise ValueError('Truncated FEX optional header')
    sections = offset + 24 + struct.unpack_from('<H', data, offset + 20)[0]
    section_count = struct.unpack_from('<H', data, offset + 6)[0]
    if sections + section_count * 40 > len(data):
        raise ValueError('Truncated FEX section table')
    trapped = virtual_counter_accesses(data)
    if trapped:
        raise ValueError(f'{path} reads registers Horizon does not let a program read: ' +
                         ', '.join(f'{register} at RVA {rva:#x}' for rva, register in trapped[:4]))
    if architecture != 'arm64ec':
        return
    load_rva = struct.unpack_from('<I', data, offset + 24 + 192)[0]
    metadata = 0
    for section in range(section_count):
        size, rva, raw_size, raw = struct.unpack_from('<IIII', data, sections + section * 40 + 8)
        if rva <= load_rva and load_rva - rva + 208 <= raw_size:
            location = raw + load_rva - rva
            if location + 208 <= len(data):
                metadata = struct.unpack_from('<Q', data, location + 200)[0]
    if not metadata:
        raise ValueError('FEX DLL lacks ARM64EC metadata')


def required_exports(architecture):
    if architecture == 'arm64ec':
        return set(re.findall(r'^@ (?:stdcall|extern) (\w+)',
                   (HORIZON_WINE.parent / 'dlls/winebox64ec/winebox64ec.spec').read_text(), re.M))
    return set(re.findall(r'GET_PTR\( (BTCpu\w+|__wine_get_unix_opcode) \)',
                          (HORIZON_WINE.parent / 'dlls/wow64/syscall.c').read_text()))


def validate_payload(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'fex-manifest.json').read_text())
    if (manifest.get('version'), manifest.get('revision')) != (VERSION, REVISION):
        raise ValueError('FEX payload does not match the pinned release')
    for key, file in (('patch_sha256', PATCH), ('abi_sha256', 'unixlib.h'),
                      ('jit_registry_sha256', 'jit_registry.h'),
                      ('code_storage_sha256', 'code_storage.h'),
                      ('cache_policy_sha256', 'cache_policy.h')):
        if manifest.get(key) != source_digest(HORIZON_WINE / 'fex' / file):
            raise ValueError(f'FEX payload does not match {file}')
    modules = manifest.get('modules', {})
    if set(modules) != set(DLLS):
        raise ValueError('FEX payload must contain both CPU modules')
    for name, architecture in DLLS.items():
        path = directory / name
        if modules[name] != {'architecture': architecture, 'sha256': digest(path)}:
            raise ValueError(f'FEX payload mismatch: {name}')
        validate_image(path, architecture)
    if not manifest.get('licenses'):
        raise ValueError('FEX payload lacks licenses')
    for name, checksum in manifest['licenses'].items():
        if Path(name).name != name or digest(directory / 'licenses' / name) != checksum:
            raise ValueError(f'Invalid FEX license: {name}')
    return manifest


def build_payload(source, arm64ec, wow64, output):
    source, output = map(Path, (source, output))
    llvm = Path(shutil.which('llvm-strip')).resolve().parents[1]
    output.mkdir(parents=True, exist_ok=True)
    modules = {}
    for dll, (name, architecture) in zip((arm64ec, wow64), DLLS.items()):
        target = output / name
        subprocess.run([str(llvm / 'bin/llvm-strip'), '--strip-debug', '-o', str(target), str(dll)], check=True)
        validate_image(target, architecture)
        info = subprocess.check_output([str(llvm / 'bin/llvm-readobj'), '--coff-imports',
                                        '--coff-exports', str(target)], text=True)
        imports = set(re.findall(r'Import \{\n  Name: (.+)', info))
        expected = {'ntdll.dll'} if architecture == 'arm64ec' else {'ntdll.dll', 'wow64.dll'}
        if imports != expected:
            raise ValueError(f'Unexpected {name} dependencies: {imports}')
        required = required_exports(architecture)
        exports = set(re.findall(r'^  Name: (.+)$', info, re.M))
        if not required <= exports:
            raise ValueError(f'{name} exports missing: {required - exports}')
        modules[name] = {'architecture': architecture, 'sha256': digest(target)}
    licenses = output / 'licenses'
    licenses.mkdir(exist_ok=True)
    sources = {'FEX-MIT.txt': source / 'LICENSE', 'FEX-LLVM.txt': llvm / 'LICENSE.TXT',
               'FEX-optparse.txt': source / 'Source/Common/cpp-optparse/LICENSE'}
    for name, file in (('fmt', 'LICENSE'), ('xxhash', 'LICENSE'), ('range-v3', 'LICENSE.txt'),
                       ('unordered_dense', 'LICENSE'), ('rpmalloc', 'LICENSE'),
                       ('tiny-json', 'LICENSE'), ('cephes', 'LICENSE')):
        sources[f'FEX-{name}.txt'] = source / 'External' / name / file
    for name, path in sources.items():
        shutil.copy2(path, licenses / name)
    softfloat = (source / 'External/SoftFloat-3e/include/SoftFloat-3e/softfloat.h').read_text()
    (licenses / 'FEX-SoftFloat.txt').write_text(softfloat.split('*/', 1)[0] + '*/\n')
    manifest = {'version': VERSION, 'revision': REVISION, 'modules': modules,
                'patch_sha256': source_digest(HORIZON_WINE / 'fex' / PATCH),
                'abi_sha256': source_digest(HORIZON_WINE / 'fex/unixlib.h'),
                'jit_registry_sha256': source_digest(HORIZON_WINE / 'fex/jit_registry.h'),
                'code_storage_sha256': source_digest(HORIZON_WINE / 'fex/code_storage.h'),
                'cache_policy_sha256': source_digest(HORIZON_WINE / 'fex/cache_policy.h'),
                'licenses': {path.name: digest(path) for path in sorted(licenses.iterdir()) if path.is_file()}}
    (output / 'fex-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    validate_payload(output)
    print(output)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('arm64ec', type=Path)
    parser.add_argument('wow64', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    build_payload(args.source, args.arm64ec, args.wow64, args.output)
