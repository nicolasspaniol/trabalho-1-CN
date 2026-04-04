import create # Importa a sessão e as libs do create
import time

session = create.get_session()

def run_cleanup(db_id, table_name, bucket_name, sg_name):
    print("🧹 INICIANDO LIMPEZA TOTAL...")
    
    rds = session.client('rds')
    dynamo = session.client('dynamodb')
    s3_res = session.resource('s3')
    ec2 = session.client('ec2')

    # 1. RDS
    try:
        rds.delete_db_instance(DBInstanceIdentifier=db_id, SkipFinalSnapshot=True)
        print("⏳ Deletando RDS (waiter)...")
        rds.get_waiter('db_instance_deleted').wait(DBInstanceIdentifier=db_id)
    except: print("⚠️ RDS não encontrado.")

    # 2. Dynamo
    try:
        dynamo.delete_table(TableName=table_name)
        print("✅ Dynamo deletado.")
    except: print("⚠️ Tabela não encontrada.")

    # 3. S3
    try:
        bucket = s3_res.Bucket(bucket_name)
        bucket.objects.all().delete()
        bucket.delete()
        print("✅ S3 limpo e deletado.")
    except: print("⚠️ Bucket não encontrado.")

    # 4. Security Group
    time.sleep(10)
    try:
        ec2.delete_security_group(GroupName=sg_name)
        print("✅ Security Group deletado.")
    except Exception as e: print(f"❌ Erro ao deletar SG: {e}")