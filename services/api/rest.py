""" Implementa uma versão simples dos métodos esperados de uma API REST """

from sqlmodel import select
from fastapi import HTTPException, status


def post(session, model, obj):
    obj_db = model.model_validate(obj)
    session.add(obj_db)
    session.commit()
    session.refresh(obj_db)
    return obj_db


def get_all(session, model, offset, limit):
    objs = session.exec(select(model).offset(offset).limit(limit)).all()
    return objs


def get_one(session, model, id):
    obj = session.get(model, id)
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    return obj


def put(session, model, id, obj):
    obj_db = session.get(model, id)
    if not obj_db:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    obj_db.sqlmodel_update(obj.model_dump())
    session.add(obj_db)
    session.commit()
    session.refresh(obj_db)
    return


def patch(session, model, id, obj):
    obj_db = session.get(model, id)
    if not obj_db:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    obj_db.sqlmodel_update(obj.model_dump(exclude_unset=True))
    session.add(obj_db)
    session.commit()
    session.refresh(obj_db)
    return


def delete(session, model, id):
    obj = session.get(model, id)
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    session.delete(obj)
    session.commit()
    return
