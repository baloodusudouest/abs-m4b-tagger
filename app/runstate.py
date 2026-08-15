"""État partagé entre la passe de fond et l'interface web.

Les deux tournent dans le même conteneur, sur des threads différents. La passe
écrit dans les fichiers audio et dans ABS ; l'interface aussi, quand tu valides
ou réidentifies. Le verrou empêche les deux d'agir en même temps sur le disque.
"""

import threading

# Pris par toute opération qui déplace des fichiers ou écrit des tags.
VERROU = threading.RLock()

# Renseigné par main.py pour que l'interface sache où en est le traitement.
ETAT = {
    "passe_en_cours": False,
    "debut": 0,
    "traites": 0,
    "total": 0,
    "phase": "démarrage",
    "derniere_fin": 0,
    "dernier_bilan": "",
}


def maj(**kwargs) -> None:
    ETAT.update(kwargs)
