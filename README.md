# About

Experimental static recompiler that ports PlayStation 3 games to native x86-64 Linux executables.

PPU (PowerPC) and SPU code is translated ahead of time to C++ and compiled together with our own LV2 kernel and
system-library runtime and a Vulkan implementation of the RSX graphics pipeline. PPU code has no interpreter
fallback; SPU code that only appears at run time (job overlays) runs in an exact interpreter until it reaches
translated code. There is no separate emulator process.

Unsupported or unexpected states strictly throw `std::runtime_error`. Unknown instructions, syscalls and
protocols stop the program explicitly; they are never silently skipped or faked.

[Knowledge base](docs/knowledge/INDEX.md) · [current state](docs/knowledge/CURRENT.md) ·
[architecture](docs/architecture.md) · [progress log](docs/menu-progress.md) ·
[agent workflow](AGENTS.md) (project documentation is written in Czech)

## Status

The first and only target so far is **Ratchet & Clank: A Crack in Time** (BCES00511, version 01.00).

- The original game boots, plays its videos, reaches the original main menu and loads a saved game into the
  first level (`great_clock_a`). The menu and the level are rendered by the Vulkan RSX backend.
- The menu runs at full speed. The best level build so far runs at about 22 FPS on a Ryzen 9 9900X with an
  RTX 4070 SUPER. Reaching a stable 60 FPS with headroom is the current work item.
- Input works through SDL game controllers; navigation with a virtual and a uinput controller is verified.
  A physical controller has not been verified yet.
- Audio is produced through SDL. Linux is the only supported host for the game runtime.

Measurements, open blockers and reproduction commands are tracked in the
[knowledge base](docs/knowledge/CURRENT.md).

## Requirements

- Linux x86-64, Python 3.11+, GCC with C++17, SDL2, a Vulkan driver (a real GPU; Lavapipe for tests).
- Optional: `cryptography` for the `unself` command (`python3 -m pip install -e .[self]`).
- **No game data, firmware, keys or system files are included.** To run a game you need your own legally
  obtained copy of the game, the PS3 system software and the decryption parameters for your copy.

## Try it without game data

```bash
mkdir -p build
python3 -m anyps3 demo -o build/answer42.elf
python3 -m anyps3 inspect build/answer42.elf
python3 -m anyps3 recompile build/answer42.elf -o build/answer42.cpp
c++ -std=c++17 -O2 build/answer42.cpp -o build/answer42
./build/answer42        # prints r3=42
```

The demo ELF is a synthetic PPU program written by this project
([examples/answer42.ppu.s](examples/answer42.ppu.s)), not a game dump. `python3 -m anyps3 --help` lists the other
commands (`analyze`, `recompile-program`, `sfo`, `unself`, `disc-inventory`, `disc-extract`, `recover-zip`).
Commands never overwrite existing output files.

## Tests

```bash
ANYPS3_REQUIRE_VULKAN=1 python3 -m unittest discover -s tests -q
```

The suite has several hundred tests (native C++ harnesses, Vulkan through Lavapipe or a real GPU) and does not
need any game files.

## Prior art

[AnyPS5](https://github.com/boykopovar/AnyPS5) inspired the way the tool is used. Public PS3 static recompilation
projects, notably [ps3recomp](https://github.com/sp00nznet/ps3recomp) (MIT), inspired some of the runtime
techniques. [RPCS3](https://github.com/RPCS3/rpcs3) (GPL-2.0) serves as a behavioural reference and is cited in
code comments where hardware or firmware behaviour was taken from it. No source files of these projects are
included.

## Disclaimer

This project is intended for interoperability, research, preservation and compatibility purposes only. It does
not include, distribute or require any copyrighted software, firmware, game data, cryptographic keys or
proprietary libraries. Users are solely responsible for obtaining any games, system software and keys legally,
and for using them and this software in accordance with the laws of their jurisdiction and the applicable
license terms. Do not use this project to infringe copyright or to circumvent technical protection measures
where doing so is unlawful.

This software is experimental and is provided **"AS IS", WITHOUT WARRANTY OF ANY KIND**, express or implied,
including but not limited to the warranties of merchantability, fitness for a particular purpose, accuracy and
non-infringement. **To the maximum extent permitted by applicable law, the authors and contributors shall not be
liable for any claim, damages or other liability**, whether direct, indirect, incidental, special, consequential
or exemplary, including but not limited to loss of data, hardware damage, lost profits or legal consequences,
arising from or in connection with the software or its use, misuse or inability to use, even if advised of the
possibility of such damage. You use this software entirely at your own risk.

This project is not affiliated with, endorsed, sponsored or approved by Sony Interactive Entertainment,
Insomniac Games or any other rights holder. "PlayStation", "PS3", "Cell Broadband Engine", "Ratchet & Clank"
and all other trademarks are the property of their respective owners and are used here only to describe
compatibility.

## License

This project is licensed under the GNU General Public License version 2 only (GPL-2.0-only).
See [LICENSE](LICENSE).
