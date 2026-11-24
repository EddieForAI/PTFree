# Third-party notices

PTFree project code uses the [Apache License 2.0](LICENSE). The following components are supplied under their own terms.

| Component | Terms and distribution |
| --- | --- |
| CPython | The Windows installation ZIP includes the official CPython 3.14.10 embeddable distribution under the Python Software Foundation license and the accompanying notices under `runtime/LICENSE.txt`. Native Linux builds, when provided, include CPython for the launcher, with its notices in `third-party-licenses/`. The 2.0.6 Linux installer uses the system Python instead. [CPython](https://www.python.org/downloads/) |
| cryptography, cffi, pycparser | Their wheel license texts accompany the Windows runtime and Linux distributions. Cryptography is dual-licensed under Apache-2.0 or BSD-3-Clause. [cryptography](https://pypi.org/project/cryptography/) · [cffi](https://pypi.org/project/cffi/) · [pycparser](https://pypi.org/project/pycparser/) |
| OpenSSL | OpenSSL used by the packaged cryptography wheels and Python runtimes is subject to its applicable upstream terms. OpenSSL license text is supplied with the package notices. [OpenSSL license](https://openssl-library.org/source/license/index.html) |
| PyInstaller | Native Linux launchers, when provided, are generated using PyInstaller. The 2.0.6 Linux installer does not contain a PyInstaller launcher. Its executable-bundle exception permits distribution under the application's own license, subject to each bundled dependency's terms. [PyInstaller license](https://pyinstaller.org/en/stable/license.html) |
| qBittorrent, RouterOS, and ImDisk | Optional integrations. These products are obtained separately. |
| M-Team favicon | The web interface requests the official site favicon directly from M-Team when its site icon is shown. The browser displays a letter M if the image is unavailable. The source and installation ZIPs contain no copy of the favicon. |
