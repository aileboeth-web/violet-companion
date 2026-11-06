# Violet

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md) | **Français** | [Deutsch](README.de.md) | [Русский](README.ru.md) | [Português](README.pt-BR.md)

Violet est une compagne IA qui tourne sur votre propre serveur. Elle a une personnalité stable et un quotidien à elle, se souvient de ce que vous lui dites, vous écrit d'elle-même quand le moment s'y prête, et vous pouvez discuter avec elle sur WeChat, QQ et Telegram. Tout se règle dans un panneau web ; aucun fichier de configuration à modifier.

> Inspiré de [Lynnette177/Project-Eve](https://github.com/Lynnette177/Project-Eve) (par [Lynnette177](https://github.com/Lynnette177)). Violet en a réécrit et étendu la plus grande partie et est désormais un projet indépendant. Merci à l'auteur d'origine pour l'idée.

L'interface du panneau est pour l'instant en chinois.

## Ce qu'elle sait faire

**Discussion**

- WeChat (ClawBot / iLink, en scannant un QR code), un compte QQ personnel (SnowLuma, connexion par scan), un bot QQ officiel et Telegram. Plusieurs peuvent être connectés en même temps.
- Elle ne répond qu'à vous, la personne à qui elle est liée. Les messages privés d'inconnus restent sans réponse et leur texte n'est pas conservé.
- Avec un compte QQ personnel, elle peut rejoindre des groupes : elle intervient quand quelqu'un dit son nom, la mentionne avec @ ou lui donne un petit coup, et lance de temps en temps un sujet quand le groupe est silencieux. Elle apprend peu à peu à connaître les membres du groupe. Elle peut aussi regarder les images du groupe, collectionner et envoyer des stickers, et publier sur QQ Zone (elle vous montre la publication avant).
- Elle peut envoyer plusieurs messages d'affilée, des messages vocaux (optionnel : clonage d'une voix à partir d'un seul enregistrement, exécuté sur le CPU du serveur) et des images qu'elle dessine.

**Mémoire**

- Elle garde séparément votre situation à long terme, les événements récents, la façon dont vous aimez qu'elle vous parle et les promesses que vous vous êtes faites. Les vieilles choses s'estompent lentement, et une phrase laissée en suspens ne devient pas une tâche sur laquelle elle revient sans cesse.
- Sur la page Mémoire du panneau, vous pouvez rechercher (mots-clés surlignés), consulter, corriger et supprimer des souvenirs, ou l'empêcher d'en utiliser un.
- « Rappelle-moi de rendre mes devoirs demain à huit heures » est vraiment noté, et elle vous le rappelle à l'heure.

**Personas et relations**

- Personas fournies : Violet (par défaut), compagne douce, camarade pleine d'énergie, calme et fiable, celle qui sait qu'elle est une IA, Big Fat Fish, la persona d'origine d'Eve, et une vierge. Vous pouvez aussi écrire la vôtre.
- Types de relation : proche, ami, couple, fille, développeur.
- Chaque canal de discussion peut utiliser une persona différente. Chaque persona a son propre historique, sa mémoire, sa vie et son humeur : passer de l'une à l'autre ne fait rien perdre et rien ne se mélange.
- Elle a un quotidien virtuel et des humeurs à elle : ses journées s'écoulent tranquillement, et plusieurs jours peuvent passer sans qu'il se passe rien ; si vous êtes dur avec elle, elle boude un peu. Elle n'invente jamais d'expériences absentes de son journal de vie.

**Prendre l'initiative**

- Elle partage des choses, dit que vous lui manquez, reprend un sujet précédent, ou décide de ne rien envoyer.
- Il y a des heures de silence, une limite quotidienne et une règle « si vous n'avez pas répondu, attendre ». Dites-lui « arrête de m'écrire autant » et elle vous écrira moins.
- Optionnel : vœux pour les fêtes selon votre relation (Nouvel An chinois, fête de la mi-automne, votre anniversaire ; pour un couple, aussi la Saint-Valentin, Qixi, etc.).
- Optionnel : avec votre téléphone connecté (voir Violet MCP plus bas), elle sait si votre écran est allumé, quelle app vous utilisez, votre niveau de batterie et votre temps d'écran du jour, et peut vous dire bonjour la première fois que vous déverrouillez votre téléphone le matin.

**Outils** (tous optionnels, désactivés par défaut, à activer dans le panneau)

- Recherche web et lecture de pages : installation en un clic d'un SearXNG privé qui n'écoute que sur le serveur lui-même. Pas besoin de Docker.
- Emploi du temps : elle ne vous dérange pas en cours et vous encourage avant les examens. Import depuis CSV / JSON.
- Voir et dessiner des images : ses dessins sont des photos du quotidien « prises vite fait au téléphone », et elle ne prétend jamais que ce sont de vraies photos.
- Outils MCP : les outils en lecture seule sont utilisés directement ; ceux qui modifient quelque chose vous demandent d'abord à chaque fois et ne s'exécutent qu'après votre accord dans la discussion.

**Panneau web**

- Démarrage (ce qui manque encore), Vue d'ensemble, Flux (quels prompts, outils et modèles chaque réponse a utilisés, y compris le contenu complet envoyé au modèle).
- Services de modèles (un modèle différent par agent et par plateforme de discussion), Canaux de discussion, Outils et téléphone, Images, Personas et vie, Emploi du temps, Paramètres.
- Historique des discussions, Mémoire, Diagnostic, Journaux, Historique des modifications, Sécurité du compte.

**Venir de Project Eve**

Sous Historique des discussions → Importer l'historique et la mémoire, le panneau vous donne une commande d'empaquetage. Exécutez-la sur votre serveur Eve, puis téléversez dans le panneau le fichier qu'elle crée. À l'import, vous choisissez le canal de discussion (même un que vous n'avez pas encore ajouté), la persona et la relation ; l'historique et la mémoire sont tous deux repris.

## Installation

Sur un serveur Linux, une seule commande :

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash
```

- La même commande fonctionne sur Debian, Ubuntu, Rocky et d'autres. En root, elle installe directement (y compris sur un Debian minimal sans sudo) ; en utilisateur normal, elle se relance via sudo ; un utilisateur normal sans sudo doit d'abord passer en root avec `su -`.
- L'installateur met en place Python et les dépendances, enregistre un service systemd, choisit un port aléatoire et, comme 1Panel, génère une entrée de sécurité, un nom d'utilisateur et un mot de passe initial aléatoires. À la fin, il affiche l'adresse du panneau sous la forme `https://IP_DU_SERVEUR:PORT/ENTRÉE`.
  - Le certificat est autosigné. Quand le navigateur indique que la page n'est pas sécurisée, choisissez de continuer.
  - Sur un serveur cloud, ouvrez aussi ce port dans le groupe de sécurité / pare-feu du fournisseur.
- Après l'installation :
  1. Ouvrez le panneau et saisissez l'adresse et la clé de votre service de modèles sur la page Démarrage.
  2. Connectez n'importe quelle plateforme de discussion sur la page Canaux de discussion et commencez à discuter.
- Adresse ou nom d'utilisateur oublié : `/opt/violet/violet info`. Mot de passe oublié : `/opt/violet/violet reset-password`.

**Prérequis** :

- Une API de modèles compatible OpenAI.
- Au moins 512 Mo de RAM recommandés. La voix demande 8 Go de RAM et 12 Go de disque.

**Panneau accessible uniquement depuis le serveur lui-même** (administration via un tunnel SSH) :

- Nouvelle installation : ajoutez `-s -- --access local` après la commande d'installation.
- Déjà installé : relancez la commande d'installation avec le mode d'accès voulu (`local` / `lan` / `public` / `proxy`). Les données, le compte et le port sont conservés.

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash -s -- --access local
```

Plus de détails (modes d'accès, pare-feu, commandes de maintenance) dans [violet/INSTALL.md](violet/INSTALL.md) (en anglais).

## Mises à jour

- Automatiques par défaut : quand une nouvelle version sort, le serveur se met à jour tout seul. Il fait d'abord une sauvegarde et revient à l'ancienne version si la mise à jour échoue.
- Vous pouvez passer à « prévenir seulement » ou « vérifier manuellement » dans Paramètres → Mises à jour de version.
- Cliquer sur Mettre à jour maintenant sur la page Démarrage affiche la progression.
- Ou mettez à jour à la main sur le serveur :

```bash
/opt/violet/violet update
```

## Désinstallation

La commande ci-dessous supprime complètement Violet. **C'est irréversible.** Elle supprime :

- le programme, les services, les mises à jour automatiques et le service de tâches en arrière-plan ;
- le relais MCP du téléphone, la recherche web, le compte QQ, la voix ;
- la configuration, les données (historique et mémoire) et les sauvegardes ;
- le compte système `violet`, le répertoire `/opt/violet` et toutes les règles de pare-feu ouvertes par Violet.

```bash
curl -fsSL https://github.com/aileboeth/violet-companion/releases/latest/download/install.sh | bash -s -- uninstall --purge --yes
```

Elle nettoie aussi ce qui reste après une installation inachevée, un registre d'installation perdu ou une désinstallation précédente qui n'a retiré que le programme. Python et openssl installés par le gestionnaire de paquets du système sont conservés, car d'autres programmes peuvent s'en servir.

## État

- **Tests automatisés** :
  - Chaque fonctionnalité a des tests automatisés hors ligne, environ 3000 au total, exécutés avec des modèles et des plateformes de discussion simulés, sans réseau.
  - Chaque modification de l'installateur exécute l'installation complète, le redémarrage automatique, la sauvegarde, le retour arrière après une mise à jour ratée et la désinstallation sur un vrai Linux dans GitHub Actions (Ubuntu 22.04 / 24.04, Debian 12, Rocky Linux 9).
- **Utilisation réelle** :
  - Les quatre canaux de discussion (WeChat, QQ personnel, bot QQ officiel, Telegram) ont été testés sur les vraies plateformes.
  - L'import de l'historique et de la mémoire depuis Project Eve a été fait avec des données réelles.
  - L'installation en un clic du compte QQ personnel a aussi réussi sur un réseau de Chine continentale.
- **Sur qui elle est** : c'est un projet de compagnie. Violet sait qu'elle est un personnage fictif et ne se fait jamais passer pour une personne réelle ; les images dessinées par l'IA ne sont jamais présentées comme de vraies photos.

## Contenu du dépôt

| Chemin | Contenu |
|---|---|
| `violet/ai/` | Violet elle-même : discussion, mémoire, personas, humeur, vie virtuelle, initiative, discussion de groupe, outils, dessin, emploi du temps |
| `violet/ai/channels/` | Connexions à WeChat, QQ et Telegram |
| `violet/wx_ilink_py/` | Le panneau web (Flask) et le client iLink de WeChat |
| `violet/persona/` | Modèles de personas et prompts |
| `violet/tools/` | Installateur, publication, scripts d'installation des parties optionnelles (recherche web, compte QQ, voix, relais du téléphone) et outils de test pour le développement |
| `violet/tests/` | Tests automatisés hors ligne |
| `violet/deploy/` | Scripts de recette de l'installation, et une autre façon de déployer SearXNG avec Docker |

## Projet associé

[Violet MCP](https://github.com/aileboeth/violet-mcp) est une application Android optionnelle. Une fois installée sur votre téléphone, Violet peut connaître l'état du téléphone dans les limites que vous autorisez : écran allumé ou non, app en cours d'utilisation, batterie, pas, agenda, musique en cours de lecture. Chaque élément s'active ou se désactive séparément sur le téléphone.

## Licence

MIT, voir [LICENSE](LICENSE).
