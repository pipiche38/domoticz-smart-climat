# Domoticz AC Pilot — plugin Domoticz

*Projet : **domoticz-ac-pilot** · [🇬🇧 English](README.md) | 🇫🇷 Français*

Un plugin Python [DomoticzEx](https://wiki.domoticz.com/Developing_a_Python_plugin) qui
ajoute un **contrôle maître** de haut niveau pour un ou plusieurs splits de climatisation
déjà pilotables dans Domoticz via des widgets existants.

Le plugin crée ses propres widgets (afin de recevoir les rappels `onCommand`) et, quand
vous les actionnez, il relaie la commande vers vos widgets de climatisation existants — référencés
par leur **idx** — via l'API JSON de Domoticz sur `127.0.0.1:8080`. Une seule action du
maître peut piloter plusieurs splits à la fois.

## Ce qu'il crée

| Unité | Type | Valeurs |
|-------|------|---------|
| **Master** | Interrupteur sélecteur | `Off` / `Cold-Auto` / `Heat-Auto` / `Manual` |
| **Target Temp** | Consigne (setpoint) | température utilisée par les modes auto |

### Comportement

| Master | Action envoyée à chaque split |
|--------|-------------------------------|
| **Off** | On/Off → Off |
| **Cold-Auto** | On/Off → On, Mode → Froid, SetTemp → Target Temp, **Ventilation → régulée** |
| **Heat-Auto** | On/Off → On, Mode → Chaud, SetTemp → Target Temp, **Ventilation → régulée** |
| **Manual** | On/Off → On uniquement — vous pilotez ensuite vous-même les widgets Mode/Ventilation/SetTemp |

Modifier **Target Temp** renvoie la consigne à chaque split **seulement tant que** le
maître est en Cold-Auto ou Heat-Auto, et relance la régulation de la ventilation. En
Manual/Off, le plugin laisse la consigne et la ventilation inchangées.

#### Unités sans interrupteur de marche (pilotées par le Mode)

Certaines unités n'ont pas d'interrupteur On/Off séparé — la marche est le premier niveau
du sélecteur de Mode (p. ex. `0` = Off, puis Froid/Chaud/…). Dans ce cas, **laissez l'idx
On/Off vide**. Le plugin gère alors la marche via le sélecteur de Mode :

- **Off** → place le Mode sur `mode.off` (défaut `0`).
- **Cold-Auto / Heat-Auto** → place le Mode sur Froid/Chaud, ce qui allume l'unité.
- **Manual** → ne fait rien (pas d'interrupteur à actionner) ; vous pilotez le Mode vous-même.

Un **idx de Ventilation reste obligatoire** pour la régulation. Vous pouvez mélanger les
types d'unités au sein d'une même instance : indiquez un idx là où un split a un
interrupteur, et rien à cette position là où il n'en a pas.

### Régulation thermostatique de la ventilation

Dans les modes auto, le plugin n'utilise **pas** une vitesse de ventilation fixe. À chaque
battement (toutes les 30 s), il lit le(s) capteur(s) de température **ambiante** de chaque
split et calcule l'erreur directionnelle :

- Cold-Auto : `erreur = ambiante − cible`
- Heat-Auto : `erreur = cible − ambiante`

L'erreur gravit l'**échelle de ventilation** — vos niveaux triés par ordre croissant, en
**excluant `auto`** — d'un cran par tranche de `FAN_STEP_DEG` (défaut `0,5` °C) :

| erreur (°C) | cran de ventilation |
|-------------|---------------------|
| ≤ 0 (à/au-delà de la cible) | cran 1 (le plus lent, p. ex. Silence) |
| 0,5 – 1,0 | cran 2 |
| 1,0 – 1,5 | cran 3 |
| … | … |
| ≥ (N−1)×0,5 | cran maximal (le plus rapide) |

La table s'adapte au nombre de niveaux configurés et plafonne au maximum. La ventilation
n'est renvoyée que lorsque le cran change réellement, donc pas d'oscillations.

Si un **idx de température extérieure** est configuré et que la charge extérieure est forte
dans le sens utile (plus chaud que la cible de plus de `EXT_BOOST_DELTA` = 8 °C en froid,
ou plus froid que la cible de plus de 8 °C en chaud), la ventilation est augmentée d'un
cran supplémentaire — mais **seulement tant que la pièce a besoin d'être corrigée**
(`erreur > 0`). Dès que la pièce atteint ou dépasse la cible, la ventilation retombe à son
cran le plus lent quelle que soit la température extérieure, afin de ne jamais continuer à
souffler quand aucun chauffage/refroidissement n'est souhaité.

Le thermostat propre de l'unité démarre/arrête toujours le compresseur à partir de la
consigne — le plugin ne gère que la **vitesse de ventilation**. Les seuils et le boost sont
des constantes en tête de `plugin.py`, faciles à ajuster.

### Respect d'une coupure externe

À chaque battement, le plugin vérifie l'état d'alimentation **réel** de chaque split —
l'interrupteur On/Off s'il est configuré, sinon le niveau du sélecteur de Mode. Si une
unité a été **éteinte de l'extérieur** (p. ex. depuis la télécommande), le plugin **se met
en retrait** : il ignore complètement ce split, n'envoie aucune commande mode/ventilation/
consigne, et ne rallume donc jamais l'unité. La régulation reprend automatiquement dès que
l'unité est de nouveau allumée (via la télécommande, ou en re-sélectionnant le mode
maître). Ceci suppose que le widget Domoticz reflète l'état réel de l'unité.

### Réduction « éco » selon l'occupation (optionnel)

Si un split a un **idx de détecteur de mouvement** configuré, le plugin suit l'occupation
de la pièce. Un mouvement détecté **verrouille la pièce comme occupée pendant
`MOTION_HOLD_MIN` (défaut 30 min)**, et chaque nouvelle détection réarme toute la fenêtre —
ce qui évite des bascules chaotiques confort↔éco. Une fois cette fenêtre écoulée sans
mouvement, la pièce est considérée vide et le split vise une température **éco** au lieu de
votre Target Temp :

- Cold-Auto vide → `ECO_COLD_TEMP` (défaut **25 °C**)
- Heat-Auto vide → `ECO_HEAT_TEMP` (défaut **18 °C**)

La consigne éco est envoyée au widget SetTemp et la ventilation se régule autour. Dès qu'un
mouvement revient, le split repasse à votre Target Temp de confort. Un split sans idx de
mouvement est toujours considéré occupé. Ces constantes sont en tête de `plugin.py`.

### Apprentissage de la montée en régime (persistant)

Quand un épisode de régulation démarre avec une grosse erreur (`WARMUP_START_ERR`, défaut
1,5 °C — p. ex. un changement de mode ou un saut d'occupation/consigne), le plugin
**pré-positionne la ventilation à un cran élevé puis la réduit** à mesure que la pièce
approche la cible, au lieu de monter lentement.

Il **apprend** aussi à quelle vitesse chaque pièce réduit cet écart (°C/min) et stocke la
valeur (une EMA) dans une **variable utilisateur** Domoticz nommée
`DomoticzACPilot_<HardwareID>`. Le même bloc contient aussi les poids de fiabilité par capteur
quand `avg` vaut `weighted`, p. ex. :

```json
{"0": {"rate": 0.42, "n": 5, "eta_rate": 0.18, "eta_n": 40, "sensors": {"424": 0.002, "1173": 1.4}}, "1": {...}}
```

`rate` est le taux de montée en régime (pré-positionne la ventilation) ; `eta_rate` est un
taux d'approche distinct, appris en continu et utilisé uniquement pour l'**ETA** du journal
de progression (voir ci-dessous). Ils sont séparés car le taux de montée est mesuré pendant
des bouffées de ventilation forcée et surestimerait la progression en régime établi.

La variable est relue au démarrage, donc le plugin est calibré dès le premier battement :

- **Pièces lentes** (faible taux appris) : montée en régime *plus* agressive la fois suivante.
- **Pièces rapides** (taux élevé) : on réduit pour ne pas souffler inutilement.
- **Pièces inconnues** : démarrent à un `DEFAULT_SLOWNESS` modéré (0,6) puis s'ajustent.

Le taux de montée est sauvegardé immédiatement à la fin d'un épisode ; les poids de
capteurs sont sauvegardés au plus une fois toutes les `LEARN_SAVE_MIN` (15 min) pour ne pas
solliciter la variable en continu.

L'apprentissage est actif par défaut. Ajoutez `"learn": false` dans le JSON des niveaux pour
le désactiver (aucune variable utilisateur n'est alors lue ni écrite). Les constantes de
réglage (`WARMUP_*`, `LEARN_ALPHA`, `SLOWNESS_MIN`, `DEFAULT_SLOWNESS`) sont en tête de
`plugin.py`. Vous pouvez inspecter ou réinitialiser les données apprises à tout moment dans
**Réglages → Plus d'options → Variables utilisateur**.

### Journalisation de la progression

Tant que le maître est en **Cold-Auto / Heat-Auto** et qu'une pièce **n'a pas atteint la
cible**, le plugin écrit une ligne concise dans le **journal Domoticz standard** (sans avoir
besoin du mode Debug), au plus une fois par minute et par split, pour suivre la convergence
d'un coup d'œil :

```
Salon Clim — Vitesse : pièce 23,8°C -> cible 22,0°C, écart 1,80°C, ventilo=40 (occupé) +boost-ext, réduit 0,30°C/min, ETA ~10 min (hist n=42) | median sur 2 : 424=23.8 1173=23.9
```

Chaque ligne indique :

- **Nom du widget** — résolu depuis l'**idx Ventilation** au démarrage (repli sur `fan <idx>`
  si l'appareil est illisible), pour nommer les pièces au lieu de les numéroter.
- **pièce → cible / écart** — l'ambiante fusionnée, la cible active et l'erreur restante.
- **ventilo / occupé|ECO / +boost-ext** — le niveau de ventilation choisi et le contexte.
- **réduit …°C/min** — la vitesse de convergence en direct mesurée depuis la ligne précédente
  (ou `dérive` / `stable` si l'écart ne se réduit pas d'une minute à l'autre).
- **ETA ~N min** — temps jusqu'à la cible. Privilégie le taux d'approche **historique**
  (`eta_rate`, appris au fil des cycles et persisté dans le JSON d'apprentissage, affiché
  `hist n=…`), donc une ETA s'affiche même quand le taux en direct est `stable` ; repli sur
  le taux en direct (`live`) tant que l'historique est insuffisant. La persistance rend l'ETA
  pertinente dès le premier battement après un redémarrage.
- **détail de la fusion** — comment les capteurs ambiants ont été combinés
  (`median`/`mean`/`weighted`, la valeur de chaque capteur, et sa part `%` en mode `weighted`).

Quand la pièce atteint la cible, une dernière ligne est journalisée puis le split se
**tait** jusqu'à ce que l'écart se rouvre, pour ne pas saturer le journal en régime établi :

```
Salon Clim — Vitesse : à la cible 22,0°C (pièce 22,1°C, écart 0,10°C) | median sur 2 : 424=22.1 1173=22.2
```

Le détail complet à chaque battement (toutes les 30 s) reste disponible au niveau **Debug**.
La cadence est la constante `STATUS_LOG_INTERVAL` (60 s) en tête de `plugin.py`.

## Installation

1. Copiez ce dossier dans le répertoire des plugins de Domoticz de sorte que le fichier soit
   à `.../plugins/domoticz-ac-pilot/plugin.py`.
2. Redémarrez Domoticz (ou rechargez les plugins Python).
3. **Réglages → Matériel**, ajoutez le type **Domoticz AC Pilot**, remplissez les paramètres,
   **Ajouter**.

## Paramètres

| Champ | Signification |
|-------|---------------|
| **On/Off idx (CSV)** | interrupteur On/Off de chaque split. **Optionnel** — laisser vide pour les unités pilotées par le Mode (voir ci-dessous) |
| **Mode idx (CSV)** | idx du sélecteur de Mode existant de chaque split |
| **Fan idx (CSV)** | idx du sélecteur de Ventilation existant de chaque split |
| **SetTemp idx (CSV)** | idx de la consigne existante de chaque split (optionnel) |
| **Ambient temp idx (CSV)** | capteur(s) de température de pièce pilotant la régulation (voir ci-dessous) |
| **External temp idx** | idx d'un capteur de température extérieure (optionnel, unique) |
| **Motion idx (CSV)** | détecteur(s) de mouvement pour la réduction éco (optionnel ; un seul = occupée) |
| **Levels (JSON)** | les **numéros de niveau** de *vos* sélecteurs Mode et Ventilation (voir ci-dessous) |
| **Debug** | journalise chaque appel JSON sortant |

Les listes d'idx sont **alignées par position** : la *i*-ième entrée de chaque liste
appartient au split *i*. Exemple pour deux splits :

```
On/Off  : 64,70
Mode    : 65,71
Fan     : 66,72
SetTemp : 67,73
Ambient : 68,74
Motion  : 69,75
```

### Levels (JSON)

Un seul objet JSON donnant les **niveaux** numériques de vos sélecteurs Mode et Ventilation
existants (les numéros de niveau, pas les libellés) :

```json
{
  "mode": {"off": 0, "cold": 30, "heat": 40},
  "fan":  {"silence": 20, "lvl1": 30, "lvl2": 40, "lvl3": 50, "lvl4": 60, "lvl5": 70}
}
```

- `mode.cold` / `mode.heat` — niveaux utilisés par **Cold-Auto** / **Heat-Auto**.
- `mode.off` *(optionnel, défaut `0`)* — le niveau **Off** du sélecteur de Mode, utilisé pour
  éteindre les **unités pilotées par le Mode** (voir ci-dessus).
- `fan` — une table nom→niveau de **chaque** niveau de ventilation. La régulation les utilise
  **triés par ordre croissant, en excluant `auto`** (Auto n'est jamais réglé automatiquement ;
  il reste disponible en usage manuel). Les noms autres que `auto` sont libres — seuls les
  niveaux comptent.
- `learn` *(optionnel, défaut `true`)* — mettre `false` pour désactiver l'apprentissage de montée en régime.
- `avg` *(optionnel, défaut `"median"`)* — comment fusionner plusieurs capteurs d'ambiance :
  `"median"`, `"mean"` ou `"weighted"` (voir ci-dessous).

## Plusieurs capteurs par pièce

Les champs **Ambient** et **Motion** acceptent plusieurs capteurs par pièce, saisis de la
même façon. La saisie dépend du nombre de splits gérés par l'instance matérielle :

- **Un seul split** (cas habituel — une instance par pièce) : listez tous les capteurs,
  séparés par des virgules. Exemple pour l'ambiance du Salon : `424, 1294, 1390, 1173`.
- **Plusieurs splits dans une instance** : les virgules séparent les splits ; utilisez `+`
  pour grouper plusieurs capteurs d'un même split. Exemple : `68+69, 74` → le split 0 utilise
  {68, 69}, le split 1 utilise {74}.

Les capteurs **Motion** sont combinés en **OU** : si *un* détecteur de la pièce se déclenche,
le verrou d'occupation de 30 minutes est réarmé. Les détecteurs inutilisables (périmés/mauvais
type) sont ignorés ; si *tous* les détecteurs d'une pièce sont inutilisables, la pièce reste
« occupée » (confort) par sécurité.

Chaque capteur d'ambiance est vérifié individuellement (type + fraîcheur 60 min) ; les capteurs
périmés ou invalides sont écartés avant fusion. La règle de fusion est la clé `avg` du JSON des
niveaux :

| `avg` | comportement |
|-------|--------------|
| `median` *(défaut)* | Valeur médiane ; pour un nombre pair, moyenne des deux du milieu (donc écarte le plus chaud et le plus froid). Robuste à un capteur défaillant, sans réglage. |
| `mean` | Moyenne arithmétique de tous les capteurs valides. |
| `weighted` | **Appris en fonctionnement :** à chaque cycle, chaque capteur est comparé au consensus du groupe (médiane) ; une variance d'écart par capteur est suivie (EMA) et les capteurs sont pondérés à l'inverse, de sorte qu'un capteur systématiquement décalé (p. ex. près d'une fenêtre) est discrètement déprécié. Les poids appris sont **persistés** (voir ci-dessus) et survivent à un redémarrage. |

Trouvez les numéros de niveau dans **Réglages → Interrupteurs → (éditer)** (colonne Niveau) ou
via `http://127.0.0.1:8080/json.htm?type=command&param=getdevices&rid=<idx>`.

## Contrôles de cohérence des capteurs

Avant d'utiliser la lecture d'un capteur, le plugin la valide :

- Un **idx de température ambiante/extérieure** doit être un appareil de température (il doit
  exposer une valeur `Temp`) ; sinon la lecture est ignorée et une erreur est journalisée.
- Un **idx de mouvement** doit être un détecteur de mouvement (`SwitchType` contient
  « Motion ») ; sinon il est ignoré.
- Tout capteur dont la **`LastUpdate` dépasse `SENSOR_TIMEOUT_MIN` (60 min)** est considéré
  comme **périmé** et sa valeur **n'est pas utilisée**.

Conséquences quand une lecture est inutilisable (périmée, mauvais type, ou illisible) :

- **Ambiance** → la ventilation de ce split est laissée inchangée pour ce cycle (pas de
  régulation sur des données périmées).
- **Extérieure** → le boost de ventilation extérieur est simplement ignoré.
- **Mouvement** → le split revient à **occupé** (Target Temp de confort), pour qu'un détecteur
  cassé ou mal typé ne laisse jamais la pièce bloquée sur la consigne éco.

Un problème donné est journalisé une fois en erreur (puis discrètement) jusqu'au rétablissement
du capteur, pour ne pas saturer le journal à chaque battement. `SENSOR_TIMEOUT_MIN` est une
constante en tête de `plugin.py`.

> Note : avec un détecteur PIR à transitions (qui ne signale qu'au début/à la fin d'un
> mouvement), une pièce vide depuis plus de 60 minutes fait apparaître le capteur comme
> « périmé », et le split revient au confort. Si vous comptez sur l'éco lors d'absences
> longues, utilisez un détecteur qui émet périodiquement, ou augmentez `SENSOR_TIMEOUT_MIN`.

## Remarques

- L'hôte/port Domoticz sont fixés à `127.0.0.1:8080` (sans authentification). Modifiez les
  constantes `DOMOTICZ_HOST` / `DOMOTICZ_PORT` en tête de `plugin.py` si besoin.
- Pour des groupes de splits indépendants, ajoutez plusieurs instances matérielles
  **Domoticz AC Pilot**.

## Vérification manuelle des points d'API

Vous pouvez confirmer que l'API JSON fonctionne avec vos vrais idx avant de vous fier au plugin :

```bash
curl "http://127.0.0.1:8080/json.htm?type=command&param=switchlight&idx=<onoff>&switchcmd=On"
curl "http://127.0.0.1:8080/json.htm?type=command&param=switchlight&idx=<mode>&switchcmd=Set%20Level&level=20"
curl "http://127.0.0.1:8080/json.htm?type=command&param=setsetpoint&idx=<settemp>&setpoint=22"
```

Chacune doit renvoyer `{"status":"OK", ...}` et faire réagir le widget correspondant.
