# -*- coding: utf-8 -*-
"""
wad2bsp.py — вставка отредактированных текстур из WAD3 в BSP-карту GoldSrc
(CS 1.6 / Half-Life) без декомпиляции и пересборки.

GUI: python wad2bsp.py
CLI: python wad2bsp.py map.bsp textures.wad [-o out.bsp] [--rebuild]

Как это работает:
  * Текстуры в BSP сопоставляются с текстурами WAD по имени (регистр не важен).
  * Совпавшие текстуры того же размера W x H перезаписываются прямо на месте:
    движок читает mip-данные по смещениям из miptex-таблицы, поэтому байты
    можно заменить, не трогая остальной файл.
  * Если размер в WAD отличается — в строгом режиме текстура пропускается
    с предупреждением; с флагом --rebuild miptex-кусок пересобирается целиком
    и дописывается в конец BSP (карта немного растёт, старый кусок остаётся
    внутри файла как мусор — это нормально и на игру не влияет).
  * Внешние текстуры BSP (которые игра ищет в WAD) в режиме --rebuild можно
    заполнить данными из WAD — они станут вшитыми.
  * Исходный BSP никогда не изменяется: результат пишется в новый файл
    (по умолчанию <имя>_mod.bsp).
"""

import os
import struct
import sys

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    HAS_TK = True
except ImportError:
    HAS_TK = False

LUMP_COUNT = 15   # у GoldSrc 15 lumps
MIPTEX_LUMP = 2   # miptex lump

MIPTEX_HEADER = 40  # name[16] + w(i32) + h(i32) + offsets[4](i32)


def _mip_sizes(w, h):
    s0 = w * h
    return s0, s0 >> 2, s0 >> 4, s0 >> 6


# ------------------------------------------------------------------- BSP
def read_miptex_entries(bsp_data):
    """
    Читает miptex-кусок BSP (lump 2). Возвращает список записей:
      name, w, h, embedded, data_abs (абс. смещение mip-данных), mips_len,
      entry_abs (абс. начало записи), entry_len (40 + mips_len для вшитых).
    Заголовок записи BSP: name[16] + width(i32) + height(i32) + offsets[4](i32).
    Смещения offsets[] бывают entry-относительные (стандарт) или
    lump-относительные (старые компиляторы) — детектим автоматически.
    Внешняя текстура (ищется в wad): все offsets нулевые.
    """
    if len(bsp_data) < 4 + LUMP_COUNT * 8:
        return []
    lump_ofs, lump_len = struct.unpack_from('<ii', bsp_data, 4 + MIPTEX_LUMP * 8)
    if lump_ofs < 0 or lump_len <= 0 or lump_ofs + lump_len > len(bsp_data):
        return []
    lump = bsp_data[lump_ofs:lump_ofs + lump_len]
    if len(lump) < 4:
        return []
    n = struct.unpack_from('<i', lump, 0)[0]
    if n <= 0 or 4 + n * 4 > len(lump):
        return []
    dataofs = [struct.unpack_from('<i', lump, 4 + i * 4)[0] for i in range(n)]
    entries = []
    for ofs in dataofs:
        if ofs < 0 or ofs + MIPTEX_HEADER > len(lump):
            continue
        raw = lump[ofs:ofs + MIPTEX_HEADER]
        name = raw[:16].split(b'\x00', 1)[0].decode('ascii', errors='replace')
        w, h, m0, m1, m2, m3 = struct.unpack_from('<6i', raw, 16)
        offs = (m0, m1, m2, m3)
        entry_abs = lump_ofs + ofs
        if all(o == 0 for o in offs):
            entries.append({'name': name, 'w': w, 'h': h, 'embedded': False,
                            'data_abs': 0, 'mips_len': 0,
                            'entry_abs': entry_abs, 'entry_len': MIPTEX_HEADER})
            continue
        s0, s1, s2, s3 = _mip_sizes(w, h)
        tail = s3 + 768  # mip3 + палитра 256*3
        bound = min(len(bsp_data), lump_ofs + lump_len)
        base = entry_abs
        end_rel = m3 + tail
        if not (base + end_rel <= bound):
            alt = lump_ofs
            if m0 == ofs + MIPTEX_HEADER and alt + end_rel <= bound:
                base = alt
            else:
                continue  # битая запись
        entries.append({'name': name, 'w': w, 'h': h, 'embedded': True,
                        'data_abs': base + m0, 'mips_len': end_rel - m0,
                        'entry_abs': entry_abs,
                        'entry_len': end_rel - (m0 - MIPTEX_HEADER)})
    return entries


# ------------------------------------------------------------------- WAD
def read_wad3_textures(wad_path):
    """
    Читает текстуры типа miptex (0x43) из WAD3.
    Возвращает список: name, w, h, data (mip-данные + палитра, байты).
    """
    with open(wad_path, 'rb') as f:
        data = f.read()
    if len(data) < 12 or data[:4] != b'WAD3':
        raise ValueError('это не WAD3-файл (ожидалась магия WAD3)')
    n, dirofs = struct.unpack_from('<ii', data, 4)
    if n <= 0 or dirofs < 0 or dirofs + n * 32 > len(data):
        raise ValueError('битый каталог WAD3')
    out = []
    for i in range(n):
        raw = data[dirofs + i * 32: dirofs + i * 32 + 32]
        filepos, disksize, size, typ, _comp, _pad = struct.unpack_from('<iiIBBH', raw, 0)
        if typ != 0x43:
            continue
        if filepos < 0 or filepos + size > len(data):
            continue
        body = data[filepos:filepos + size]
        if len(body) < MIPTEX_HEADER:
            continue
        name = body[:16].split(b'\x00', 1)[0].decode('ascii', errors='replace')
        w, h, m0, m1, m2, m3 = struct.unpack_from('<6i', body, 16)
        if w <= 0 or h <= 0:
            continue
        s0, s1, s2, s3 = _mip_sizes(w, h)
        sizes = (s0, s1, s2, s3)
        offs = (m0, m1, m2, m3)
        if m0 != MIPTEX_HEADER and not all(o >= 0 for o in offs):
            continue
        try:
            mips = b''.join(body[o:o + s] for o, s in zip(offs, sizes))
        except Exception:
            continue
        if len(mips) != sum(sizes):
            continue
        palette = body[-768:]
        out.append({'name': name, 'w': w, 'h': h,
                    'data': mips + palette, 'palette': palette})
    return out


# ---------------------------------------------------------------- вставка
def _is_transparent(name, palette):
    """Прозрачные текстуры начинаются с '{'; индекс 255 палитры — чистый синий."""
    return name.startswith('{') and palette[765:768] != b'\x00\x00\xff'


def _pack_entry(name, w, h, data):
    """Собирает запись BSP-формата: заголовок 40 байт + mip-данные + палитра."""
    s0, s1, s2, s3 = _mip_sizes(w, h)
    offs = (MIPTEX_HEADER, MIPTEX_HEADER + s0, MIPTEX_HEADER + s0 + s1,
            MIPTEX_HEADER + s0 + s1 + s2)
    name_b = name.encode('ascii', errors='replace')[:16].ljust(16, b'\x00')
    return name_b + struct.pack('<6i', w, h, *offs) + data


def insert_wad_into_bsp(bsp_path, wad_path, out_path, rebuild=False, log=print):
    """
    Вставляет текстуры из WAD в BSP. Возвращает словарь статистики:
      replaced, filled, skipped_size, skipped_external, wad_not_found,
      untouched, external_total, out_path.
    """
    with open(bsp_path, 'rb') as f:
        bsp_data = f.read()
    entries = read_miptex_entries(bsp_data)
    if not entries:
        raise ValueError('miptex-кусок BSP не читается')
    wad_tex = read_wad3_textures(wad_path)
    if not wad_tex:
        raise ValueError('в WAD не найдено текстур типа miptex')
    wad_by_name = {t['name'].upper(): t for t in wad_tex}

    log(f'BSP: {bsp_path} (текстур в miptex-таблице: {len(entries)})')
    log(f'WAD: {wad_path} (miptex-текстур: {len(wad_tex)})')
    log('-' * 64)

    out = bytearray(bsp_data)
    stats = {'replaced': 0, 'filled': 0, 'skipped_size': 0,
             'skipped_external': 0, 'wad_not_found': 0,
             'untouched': 0, 'external_total': 0}
    matched_names = set()

    for e in entries:
        t = wad_by_name.get(e['name'].upper())
        if t is None:
            if e['embedded']:
                stats['untouched'] += 1
                log(f'  [=] {e["name"]} — нет в WAD, оставляем как есть')
            else:
                stats['external_total'] += 1
                log(f'  [ext] {e["name"]} — внешняя текстура, в WAD не найдена')
            continue
        matched_names.add(t['name'].upper())
        if not e['embedded']:
            if not rebuild:
                stats['skipped_external'] += 1
                log(f'  [~] {e["name"]} — внешняя текстура; вставка возможна '
                    f'только с --rebuild')
            continue
        if (t['w'], t['h']) != (e['w'], e['h']):
            if not rebuild:
                stats['skipped_size'] += 1
                log(f'  [!!] {e["name"]} — размер в WAD {t["w"]}x{t["h"]} не совпадает '
                    f'с BSP {e["w"]}x{e["h"]} — пропущена (нужен --rebuild)')
            else:
                log(f'  [~] {e["name"]} — размер в WAD {t["w"]}x{t["h"]} отличается '
                    f'от BSP {e["w"]}x{e["h"]} — оставлена старая версия')
            continue
        if len(t['data']) != e['mips_len']:
            stats['skipped_size'] += 1
            log(f'  [!!] {e["name"]} — размер данных WAD не совпадает с BSP, пропущена')
            continue
        if _is_transparent(e['name'], t['palette']):
            log(f'  [!] {e["name"]} — прозрачная текстура: индекс 255 палитры '
                f'в WAD не чистый синий, прозрачность может пропасть')
        out[e['data_abs']:e['data_abs'] + e['mips_len']] = t['data']
        stats['replaced'] += 1
        mode = 'заменена на месте' if (t['w'], t['h']) == (e['w'], e['h']) else '?'
        log(f'  [OK] {e["name"]} ({e["w"]}x{e["h"]}) — {mode}')

    # внешние текстуры, найденные в WAD, при rebuild — вшиваем
    if rebuild:
        new_entries = []
        for e in entries:
            t = wad_by_name.get(e['name'].upper())
            if e['embedded'] and t is not None and (t['w'], t['h']) == (e['w'], e['h']) \
                    and len(t['data']) == e['mips_len']:
                new_entries.append(_pack_entry(e['name'], e['w'], e['h'], t['data']))
            elif e['embedded']:
                # оригинал: заголовок (со стандартными смещениями) + данные как есть
                raw = bsp_data[e['entry_abs']:e['entry_abs'] + e['entry_len']]
                header = raw[:16] + struct.pack('<2i', e['w'], e['h'])
                s0, s1, s2, s3 = _mip_sizes(e['w'], e['h'])
                offs = (MIPTEX_HEADER, MIPTEX_HEADER + s0,
                        MIPTEX_HEADER + s0 + s1, MIPTEX_HEADER + s0 + s1 + s2)
                mip = raw[MIPTEX_HEADER:MIPTEX_HEADER + e['mips_len']]
                new_entries.append(header + struct.pack('<4i', *offs) + mip)
            else:
                if t is not None:
                    if _is_transparent(e['name'], t['palette']):
                        log(f'  [!] {e["name"]} — прозрачная текстура: индекс 255 '
                            f'палитры в WAD не чистый синий')
                    new_entries.append(_pack_entry(e['name'], t['w'], t['h'], t['data']))
                    stats['filled'] += 1
                    log(f'  [OK] {e["name"]} — внешняя текстура заполнена из WAD '
                        f'({t["w"]}x{t["h"]})')
                else:
                    new_entries.append(e['name'].encode('ascii', errors='replace')[:16]
                                       .ljust(16, b'\x00')
                                       + struct.pack('<6i', e['w'], e['h'], 0, 0, 0, 0))
        # собираем новый miptex-кусок: заголовок + таблица + записи (entry-относит.)
        n = len(new_entries)
        table_size = 4 + n * 4
        offsets, pos = [], table_size
        for body in new_entries:
            offsets.append(pos)
            pos += len(body)
        lump = struct.pack('<i', n) + struct.pack(f'<{n}i', *offsets) + b''.join(new_entries)
        new_lump_ofs = len(out)
        out += lump
        struct.pack_into('<ii', out, 4 + MIPTEX_LUMP * 8, new_lump_ofs, len(lump))
        log(f'Miptex-кусок пересобран: {n} текстур, дописан в конец файла '
            f'(карта выросла на {len(lump)} байт; старый кусок остался как мусор)')

    for t in wad_tex:
        if t['name'].upper() not in matched_names:
            stats['wad_not_found'] += 1
            log(f'  [-] {t["name"]} — в BSP такой текстуры нет, пропущена')

    with open(out_path, 'wb') as f:
        f.write(bytes(out))

    # самопроверка: перечитываем результат нашей же читалкой
    with open(out_path, 'rb') as f:
        chk = f.read()
    chk_entries = read_miptex_entries(chk)
    emb = sum(1 for e in chk_entries if e['embedded'])
    ext = len(chk_entries) - emb
    log('-' * 64)
    log(f'Самопроверка результата: текстур {len(chk_entries)} '
        f'(вшито {emb}, внешних {ext})')
    if not chk_entries:
        log('  [!] ВНИМАНИЕ: результат не читается — используйте резервную копию!')
    log(f'Сохранено: {out_path}')

    stats['out_path'] = out_path
    return stats


# ------------------------------------------------------------------- CLI
def main_cli(argv):
    bsp, wad, out, rebuild = None, None, None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.lower().endswith('.bsp') and bsp is None:
            bsp = a
        elif a.lower().endswith('.wad') and wad is None:
            wad = a
        elif a == '-o':
            out = argv[i + 1]
            i += 1
        elif a == '--rebuild':
            rebuild = True
        i += 1
    if not (bsp and wad):
        print(__doc__)
        return 1
    if not out:
        name = os.path.splitext(bsp)[0]
        out = name + '_mod.bsp'
    insert_wad_into_bsp(bsp, wad, out, rebuild=rebuild)
    return 0


# ------------------------------------------------------------------- GUI
def run_gui():
    root = tk.Tk()
    root.title('wad2bsp — вставка WAD в BSP (CS 1.6)')
    root.resizable(False, False)

    frm = ttk.Frame(root, padding=10)
    frm.grid(sticky='nsew')

    bsp_var, wad_var, out_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
    rebuild_var = tk.BooleanVar(value=False)

    def on_bsp_change(*_):
        bsp = bsp_var.get()
        if bsp and os.path.isfile(bsp):
            name = os.path.splitext(bsp)[0]
            if not out_var.get():
                out_var.set(name + '_mod.bsp')

    bsp_var.trace_add('write', on_bsp_change)

    def pick_bsp():
        p = filedialog.askopenfilename(title='BSP-карта',
                                       filetypes=[('BSP карты', '*.bsp')])
        if p:
            bsp_var.set(p)

    def pick_wad():
        p = filedialog.askopenfilename(title='WAD с отредактированными текстурами',
                                       filetypes=[('WAD файлы', '*.wad')])
        if p:
            wad_var.set(p)

    def pick_out():
        p = filedialog.asksaveasfilename(title='Куда сохранить BSP',
                                         defaultextension='.bsp',
                                         filetypes=[('BSP карты', '*.bsp')])
        if p:
            out_var.set(p)

    rows = [('BSP-карта:', bsp_var, pick_bsp),
            ('WAD с текстурами:', wad_var, pick_wad),
            ('Результат (BSP):', out_var, pick_out)]
    for i, (label, var, cmd) in enumerate(rows):
        ttk.Label(frm, text=label).grid(row=i, column=0, sticky='w', pady=2)
        ttk.Entry(frm, textvariable=var, width=58).grid(row=i, column=1, padx=4, pady=2)
        ttk.Button(frm, text='Обзор...', command=cmd).grid(row=i, column=2, pady=2)

    text = tk.Text(frm, height=20, width=90, font=('Consolas', 9))
    text.grid(row=3, column=0, columnspan=3, pady=(8, 4))
    scroll = ttk.Scrollbar(frm, command=text.yview)
    scroll.grid(row=3, column=3, sticky='ns')
    text.config(yscrollcommand=scroll.set)

    # копирование из журнала: правый клик, Ctrl+A, Ctrl+C
    log_menu = tk.Menu(text, tearoff=0)
    log_menu.add_command(label='Копировать',
                         command=lambda: text.event_generate('<<Copy>>'))
    log_menu.add_command(label='Выделить всё',
                         command=lambda: text.tag_add('sel', '1.0', 'end'))
    text.bind('<Button-3>', lambda e: log_menu.tk_popup(e.x_root, e.y_root))
    text.bind('<Control-a>',
              lambda e: (text.tag_add('sel', '1.0', 'end'), 'break')[1])
    text.bind('<Control-c>', lambda e: text.event_generate('<<Copy>>'))

    def do_insert():
        text.delete('1.0', 'end')
        bsp, wad = bsp_var.get(), wad_var.get()
        if not (bsp and os.path.isfile(bsp) and wad and os.path.isfile(wad)):
            messagebox.showwarning('wad2bsp', 'Укажи BSP-карту и WAD-файл.')
            return
        out = out_var.get() or os.path.splitext(bsp)[0] + '_mod.bsp'
        try:
            insert_wad_into_bsp(bsp, wad, out, rebuild=rebuild_var.get(),
                                log=lambda s: (text.insert('end', s + '\n'),
                                               text.see('end'),
                                               root.update_idletasks()))
        except Exception as e:
            messagebox.showerror('wad2bsp', str(e))

    ttk.Checkbutton(frm, text='Разрешить изменение размеров (--rebuild): '
                    'miptex-кусок пересобирается, карта слегка растёт',
                    variable=rebuild_var).grid(row=4, column=0, columnspan=3, sticky='w')
    ttk.Button(frm, text='Вставить текстуры', command=do_insert)\
        .grid(row=5, column=0, columnspan=3, pady=(4, 0))

    root.mainloop()


def main():
    if len(sys.argv) > 1:
        sys.exit(main_cli(sys.argv[1:]))
    if HAS_TK:
        run_gui()
    else:
        print(__doc__)
        sys.exit('tkinter недоступен — используй режим командной строки')


if __name__ == '__main__':
    main()
