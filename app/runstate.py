"""État partagé entre la passe de fond et l'interface web.

Les deux tournent dans le même conteneur, sur des threads différents. La passe
écrit dans les fichiers audio et dans ABS ; l'interface aussi, quand tu valides
ou réidentifies. Le verrou empêche les deux d'agir en même temps sur le disque.
"""

import os
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


# Chemins retirés de la bibliothèque par l'interface pendant la passe en cours.
# La passe travaille sur une liste d'items figée à son démarrage : sans ce
# registre, elle signalerait en ERREUR des fichiers que l'utilisateur vient
# délibérément de mettre de côté.
DEPLACES = set()


def maj(**kwargs) -> None:
    ETAT.update(kwargs)


def note_deplacement(chemin: str) -> None:
    if chemin:
        with VERROU:
            DEPLACES.add(os.path.normpath(chemin))


def deplace_par_interface(chemin: str) -> bool:
    """Le chemin a-t-il été mis de côté depuis l'interface ?"""
    if not DEPLACES:
        return False
    cible = os.path.normpath(chemin)
    for source in DEPLACES:
        if cible == source or cible.startswith(source + os.sep):
            return True
    return False


def oublier_deplacements() -> None:
    """À appeler en fin de passe : la liste d'items sera rechargée."""
    with VERROU:
        DEPLACES.clear()
