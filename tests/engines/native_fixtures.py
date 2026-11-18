"""Small authored CFB/BIFF fixtures, no third-party document content."""
import struct


def ole_container(streams):
    end, free = 0xfffffffe, 0xffffffff
    header = bytearray(512)
    header[:8] = bytes.fromhex('d0cf11e0a1b11ae1')
    struct.pack_into('<HHHH', header, 24, 0x3e, 3, 0xfffe, 9)
    struct.pack_into('<H', header, 32, 6)
    struct.pack_into('<IIIIIIIII', header, 40, 0, 1, 0, 0, 4096, end, 0, end, 0)
    directory = bytearray(512)
    def entry(index, name, kind, start, size, right=free):
        offset = index * 128
        encoded = (name + '\0').encode('utf-16le')
        directory[offset:offset+len(encoded)] = encoded
        struct.pack_into('<HBBIII', directory, offset+64, len(encoded), kind, 1, free, right, 1 if kind == 5 else free)
        struct.pack_into('<IQ', directory, offset+116, start, size)
    entry(0, 'Root Entry', 5, end, 0)
    fat = [end]
    pieces = []
    next_sector = 1
    for index, (name, data) in enumerate(streams.items(), 1):
        size = max(4096, ((len(data)+511)//512)*512)
        count = size//512
        entry(index, name, 2, next_sector, size, index+1 if index < len(streams) else free)
        fat.extend([*range(next_sector+1, next_sector+count), end])
        pieces.append(data.ljust(size, b'\0'))
        next_sector += count
    struct.pack_into('<109I', header, 76, next_sector, *([free]*108))
    fat.append(0xfffffffd)
    return bytes(header + directory) + b''.join(pieces) + struct.pack('<128I', *fat, *([free]*(128-len(fat))))


def biff_workbook():
    def record(kind, data=b''):
        return struct.pack('<HH', kind, len(data)) + data
    def bof(kind):
        return record(0x0809, struct.pack('<HHHHII', 0x0600, kind, 0xdbb, 1997, 0x41, 6))
    prefix = bof(5) + record(0x0042, struct.pack('<H', 1200)) + record(0x00e0, b'\0'*20)
    label = b'Original'
    sheet_offset = len(prefix) + 4+8+len(label) + 4
    prefix += record(0x0085, struct.pack('<IBBBB', sheet_offset, 0, 0, len(label), 0) + label) + record(0x000a)
    sheet = bof(0x10) + record(0x0200, struct.pack('<IIHHH', 0, 2, 0, 2, 0))
    sheet += record(0x0203, struct.pack('<HHHd', 0, 0, 0, 7)) + record(0x0203, struct.pack('<HHHd', 1, 0, 0, 11)) + record(0x000a)
    return prefix + sheet
