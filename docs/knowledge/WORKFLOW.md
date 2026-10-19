# Pravidla práce se znalostmi

## Jak vzniká ověřený záznam

Každý technický závěr uveď se stavem **fakt (ověřené)**, **odvozené**, **hypotéza**, **rozhodnutí** nebo **otevřená otázka**, v souladu s kořenovým `AGENTS.md`. Připoj rozsah: veřejná architektura CELL, konkrétní referenční implementace, AnyPS3 staticky, nebo konkrétní runtime běh. Shoda dvou emulátorů sama o sobě není důkazem skutečného hardwaru.

Fakt z manuálu musí mít titul, verzi, fyzickou PDF stranu, případně tištěnou stranu/sekci, cestu k původnímu PDF a SHA256 z manifestu. Zdrojový fakt má `repo-relative-path:line`, revizi referenčního klonu a vysvětlení, co přesně daný řádek dokládá. Při změně revize znovu vyhledej symbol; neudržuj staré řádky jako domněle platné.

Runtime fakt má reprodukovatelný příkaz, datum, revizi nebo hash změněných souborů, profil/hru bez kopírování chráněných dat, log, exit stav/timeout a relevantní čítače/artefakty. Uveď, zda nastal skutečný guest draw, host prezentace, clear, flip, nebo jen vznikl soubor obrázku. Černý snímek není důkazem vykresleného menu.

Hypotéza musí mít důvod, alternativu a další rozlišující test. Povýš ji na fakt jen po získání důkazu. Vyvrácenou hypotézu označ a ponech její stručný důvod, aby ji další běh neopakoval. Chyba veřejného mirroru patří do zdrojového manifestu, nikoli do seznamu příčin selhání hry.

## Pracovní smyčka

1. Přečíst aktuální stav a poslední důkaz, určit nejbližší blokující guest krok.
2. Najít konkrétní instrukci, HLE funkci, adresu, FIFO metodu nebo chybějící stav. Ověřit manuál, potom relevantní referenční implementaci.
3. Zapsat hypotézu a očekávanou změnu pozorování. Zvolit malou změnu s jasným ověřením; neobcházet skutečnou guest větev tvrzením, že menu funguje.
4. Spustit přiměřené testy a skutečný profil/běh, porovnat stejné čítače a výstup.
5. Zapsat výsledek, původ důkazu, omezení a další krok. Odkázat nový záznam z CURRENT.

## Trvalé předání

CURRENT obsahuje jen aktuální cíl, poslední platný důkaz, aktivní otázky a další krok; dlouhé logy patří do artefaktů. Témata CELL/SPU/PPU/RSX obsahují stabilnější závěry a mapu zdrojů. Záznamy jednotlivých experimentů lze ukládat jako `docs/knowledge/experiments/YYYY-MM-DD-short-name.md` a odkazovat je z CURRENT.

Průběžně zapisuj změnu rozhodujícího blokátoru; nečekej až na ukončení modelu. Před předáním odliš provedené změny, skutečně spuštěné ověření a plán. Neuváděj test jako úspěšný pouze proto, že byl připraven. Za konec cíle se počítá původní menu ACIT a ověřená gamepad interakce, nikoli pouhé dosažení bootu, callbacku nebo flipu.

Nezapisuj projektovou znalost do uživatelských systémových memories. Nezahrnuj tajemství, klíče, obsah hry, firmware ani proprietární SDK. Veřejně dostupné manuály v lokální knihovně zůstávají s původním copyrightem; před veřejnou redistribucí posuď jejich vlastní podmínky.
