"""Cercle rouge affiché brièvement à l'endroit où l'agent clique (lancé par gateway/tools/pc.py).

Usage : pythonw click_ring.pyw X Y [MS] — pixels physiques. Fenêtre au premier plan, transparente aux clics, qui ne
prend jamais le focus : elle montre le clic sans le gêner."""

import ctypes
import sys
import tkinter as tk

SIZE, COLOR, KEY = 64, "#ff2d2d", "#010203"

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # mêmes pixels que la souris
except (AttributeError, OSError):
    pass
x, y = int(sys.argv[1]), int(sys.argv[2])
ms = int(sys.argv[3]) if len(sys.argv) > 3 else 700

root = tk.Tk()
root.withdraw()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.attributes("-transparentcolor", KEY)
root.geometry(f"{SIZE}x{SIZE}+{x - SIZE // 2}+{y - SIZE // 2}")
canvas = tk.Canvas(root, width=SIZE, height=SIZE, bg=KEY, highlightthickness=0)
canvas.pack()
canvas.create_oval(4, 4, SIZE - 4, SIZE - 4, outline=COLOR, width=4)
canvas.create_oval(SIZE // 2 - 22, SIZE // 2 - 22, SIZE // 2 + 22, SIZE // 2 + 22, outline=COLOR, width=2)
root.update_idletasks()
hwnd = int(root.wm_frame(), 16)
user32 = ctypes.windll.user32
# WS_EX_TRANSPARENT (clics traversants) | WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW (pas dans la barre)
user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | 0x20 | 0x80000 | 0x08000000 | 0x80)
root.deiconify()
root.after(ms, root.destroy)
root.mainloop()
