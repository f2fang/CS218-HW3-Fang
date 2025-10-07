#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS218 HW3 - boto3 automation (create / collect / teardown)

Usage examples:
  python hw3.py create  --region us-west-1 --prefix fang --key-name fang-key
  python hw3.py collect --region us-west-1 --prefix fang
  python hw3.py teardown --region us-west-1 --prefix fang
"""

import argparse, json, sys
from pathlib import Path
import boto3
from botocore.exceptions import ClientError

def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)

import time

def tag_name(ec2, resource_id, name, retries=5):
    """Tag any EC2/VPC resource with Name=<name>, with light retry for eventual consistency."""
    for attempt in range(retries):
        try:
            ec2.create_tags(Resources=[resource_id], Tags=[{"Key": "Name", "Value": name}])
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            # Sometimes the ID isn’t immediately taggable right after creation.
            if code in {
                "InvalidVpcID.NotFound",
                "InvalidSubnetID.NotFound",
                "InvalidRouteTableID.NotFound",
                "InvalidInternetGatewayID.NotFound",
                "InvalidGroup.NotFound",
                "InvalidNatGatewayID.NotFound",
            } and attempt < retries - 1:
                time.sleep(1 + attempt)  # backoff and retry
                continue
            raise


# ---------------- CREATE ----------------
def create(args):
    region = args.region
    prefix = args.prefix
    key_name = args.key_name or "ff-test"

    ec2 = boto3.client("ec2", region_name=region)

    # 1. Create VPC
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    tag_name(ec2, vpc_id, f"{prefix}-vpc")
    print("VPC:", vpc_id)

    # 2. Create Subnets
    pub_subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24", AvailabilityZone=f"{region}a")["Subnet"]["SubnetId"]
    tag_name(ec2, pub_subnet, f"{prefix}-public-subnet")

    pri_subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.2.0/24", AvailabilityZone=f"{region}c")["Subnet"]["SubnetId"]
    tag_name(ec2, pri_subnet, f"{prefix}-private-subnet")
    print("Subnets:", pub_subnet, pri_subnet)

    # Enable auto-assign public IP on public subnet
    ec2.modify_subnet_attribute(SubnetId=pub_subnet, MapPublicIpOnLaunch={'Value': True})

    # 3. Create IGW
    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    tag_name(ec2, igw_id, f"{prefix}-igw")
    print("IGW:", igw_id)

    # 4. Allocate EIP + NAT GW
    alloc_id = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat_gw_id = ec2.create_nat_gateway(SubnetId=pub_subnet, AllocationId=alloc_id)["NatGateway"]["NatGatewayId"]
    tag_name(ec2, nat_gw_id, f"{prefix}-natgw")
    print("NATGW:", nat_gw_id)

    waiter = ec2.get_waiter("nat_gateway_available")
    waiter.wait(NatGatewayIds=[nat_gw_id])

    # 5. Route Tables
    # Find main RTB
    rts = ec2.describe_route_tables(Filters=[{'Name':'vpc-id','Values':[vpc_id]}])['RouteTables']
    main_rtb = next(rt['RouteTableId'] for rt in rts if any(a.get('Main') for a in rt.get('Associations', [])))

    # Use main RTB as public
    tag_name(ec2, main_rtb, f"{prefix}-main-RTB")
    ec2.associate_route_table(RouteTableId=main_rtb, SubnetId=pub_subnet)
    try:
        ec2.create_route(RouteTableId=main_rtb, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    except ClientError:
        pass

    # Private RTB
    rtb_pri = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    tag_name(ec2, rtb_pri, f"{prefix}-rtb-private")
    ec2.associate_route_table(RouteTableId=rtb_pri, SubnetId=pri_subnet)
    try:
        ec2.create_route(RouteTableId=rtb_pri, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_gw_id)
    except ClientError:
        pass

    print("RouteTables:", main_rtb, rtb_pri)

    # 6. Security Groups
    sg_pub = ec2.create_security_group(GroupName=f"{prefix}-sg-public", Description="Public SG", VpcId=vpc_id)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=sg_pub, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}]
    }])
    print("SecurityGroup Public:", sg_pub)

    sg_pri = ec2.create_security_group(GroupName=f"{prefix}-sg-private", Description="Private SG", VpcId=vpc_id)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=sg_pri, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
        "UserIdGroupPairs": [{"GroupId": sg_pub}]
    }])
    print("SecurityGroup Private:", sg_pri)

    # 7. Launch EC2 instances
    ami = "ami-0b09bf4b909f29738"  # AMI
    ud = """#!/bin/bash
    yum update -y
    """

    # Public EC2
    pub_run = ec2.run_instances(
        ImageId=ami, InstanceType="t3.micro", MinCount=1, MaxCount=1,
        KeyName=key_name, SubnetId=pub_subnet, SecurityGroupIds=[sg_pub],
        TagSpecifications=[{'ResourceType':'instance','Tags':[{'Key':'Name','Value':f'{prefix}-ec2-public'}]}],
        UserData=ud
    )["Instances"][0]

    # Private EC2
    pri_run = ec2.run_instances(
        ImageId=ami, InstanceType="t3.micro", MinCount=1, MaxCount=1,
        KeyName=key_name, SubnetId=pri_subnet, SecurityGroupIds=[sg_pri],
        TagSpecifications=[{'ResourceType':'instance','Tags':[{'Key':'Name','Value':f'{prefix}-ec2-private'}]}],
        UserData=ud
    )["Instances"][0]

    print("EC2:", pub_run["InstanceId"], pri_run["InstanceId"])

# ---------------- COLLECT ----------------
def collect(args):
    region = args.region
    prefix = args.prefix

    ec2 = boto3.client("ec2", region_name=region)
    sts = boto3.client("sts", region_name=region)

    # 1) caller identity
    ident = sts.get_caller_identity()
    write_json(f"{prefix}-caller-identity.json", ident)
    print("Saved:", f"{prefix}-caller-identity.json")

    # 2) find VPC by Name tag
    vpcs = ec2.describe_vpcs(Filters=[{"Name":"tag:Name","Values":[f"{prefix}-vpc"]}])["Vpcs"]
    if not vpcs:
        raise SystemExit(f"No VPC with Name tag '{prefix}-vpc' found in region {region}.")
    vpc_id = vpcs[0]["VpcId"]

    # 3) instances (filtered by VPC)
    inst = ec2.describe_instances(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])
    write_json(f"{prefix}-instances.json", inst)
    print("Saved:", f"{prefix}-instances.json")

    # 4) subnets (filtered by VPC)
    subnets = ec2.describe_subnets(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])
    write_json(f"{prefix}-subnets.json", subnets)
    print("Saved:", f"{prefix}-subnets.json")

    # 5) route tables (filtered by VPC)
    rtb = ec2.describe_route_tables(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])
    write_json(f"{prefix}-route-tables.json", rtb)
    print("Saved:", f"{prefix}-route-tables.json")

    print("All outputs collected. Put them in GDoc in order: caller-identity → iid-public/private → subnets → route-tables.")


# ---------------- TEARDOWN ----------------

def teardown(args):
    region = args.region
    prefix = args.prefix
    ec2 = boto3.client("ec2", region_name=region)

    def log(msg):  # simple logger
        print(msg, flush=True)

    def try_do(msg, fn, **kw):
        try:
            out = fn(**kw)
            log(f"✓ {msg}")
            return out
        except ClientError as e:
            err = e.response.get("Error", {}).get("Message", str(e))
            log(f"... {msg} -> {err}")
        except Exception as e:
            log(f"... {msg} -> {e}")

    # --- Resolve VPC by Name tag -------------------------------------------------
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [f"{prefix}-vpc"]}])["Vpcs"]
    if not vpcs:
        log(f"No VPC found with tag Name={prefix}-vpc in {region}. Nothing to do.")
        return
    vpc_id = vpcs[0]["VpcId"]
    log(f"VPC: {vpc_id}")

    # --- 1) Terminate all instances in this VPC ----------------------------------
    res = ec2.describe_instances(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Reservations"]
    inst_ids = [i["InstanceId"] for r in res for i in r.get("Instances", []) if i["State"]["Name"] != "terminated"]
    if inst_ids:
        try_do(f"terminate instances {inst_ids}", ec2.terminate_instances, InstanceIds=inst_ids)
        try:
            ec2.get_waiter("instance_terminated").wait(InstanceIds=inst_ids)
            log("✓ instances terminated (waiter)")
        except ClientError as e:
            log(f"... instance waiter -> {e}")

    # --- 2) Delete NAT Gateways and wait -----------------------------------------
    ngws = ec2.describe_nat_gateways(Filter=[{"Name": "vpc-id", "Values": [vpc_id]}])["NatGateways"]
    for ngw in ngws:
        ngw_id = ngw["NatGatewayId"]
        try_do(f"delete NAT GW {ngw_id}", ec2.delete_nat_gateway, NatGatewayId=ngw_id)
    if ngws:
        try:
            ec2.get_waiter("nat_gateway_deleted").wait(NatGatewayIds=[ngw["NatGatewayId"] for ngw in ngws])
            log("✓ NAT GW(s) deleted (waiter)")
        except ClientError as e:
            log(f"... NAT GW waiter -> {e}")

    # --- 3) Disassociate any public IPs/EIPs from ENIs in the VPC ----------------
    nis = ec2.describe_network_interfaces(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["NetworkInterfaces"]
    for ni in nis:
        assoc = ni.get("Association") or {}
        assoc_id = assoc.get("AssociationId")
        alloc_id = assoc.get("AllocationId")
        pub_ip   = assoc.get("PublicIp")
        if pub_ip:
            if assoc_id:
                try_do(f"disassociate address {assoc_id} from ENI {ni['NetworkInterfaceId']}",
                       ec2.disassociate_address, AssociationId=assoc_id)
            if alloc_id:
                try_do(f"release EIP {alloc_id}", ec2.release_address, AllocationId=alloc_id)

    # --- 4) Detach & delete Internet Gateways ------------------------------------
    igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])["InternetGateways"]
    for igw in igws:
        igw_id = igw["InternetGatewayId"]
        for att in igw.get("Attachments", []):
            try_do(f"detach IGW {igw_id} from VPC {att['VpcId']}",
                   ec2.detach_internet_gateway, InternetGatewayId=igw_id, VpcId=att["VpcId"])
        try_do(f"delete IGW {igw_id}", ec2.delete_internet_gateway, InternetGatewayId=igw_id)

    # --- 5) Disassociate & delete non-main route tables ---------------------------
    rtbs = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
    for rtb in rtbs:
        rtb_id = rtb["RouteTableId"]
        is_main = any(a.get("Main") for a in rtb.get("Associations", []))
        # Disassociate all non-main associations first
        for assoc in rtb.get("Associations", []):
            if assoc.get("Main"):
                continue
            assoc_id = assoc.get("RouteTableAssociationId")
            if assoc_id:
                try_do(f"disassociate RTB {rtb_id} assoc {assoc_id}",
                       ec2.disassociate_route_table, AssociationId=assoc_id)
        # Remove 0.0.0.0/0 route if present (helps IGW/NAT dependencies)
        for route in rtb.get("Routes", []):
            if route.get("DestinationCidrBlock") == "0.0.0.0/0":
                try_do(f"delete default route from {rtb_id}",
                       ec2.delete_route, RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0")
        # Delete the RTB if it’s not main
        if not is_main:
            try_do(f"delete RTB {rtb_id}", ec2.delete_route_table, RouteTableId=rtb_id)

    # --- 6) Delete subnets --------------------------------------------------------
    subs = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    # Ensure no leftover ENIs block subnet deletion
    for sn in subs:
        sn_id = sn["SubnetId"]
        # extra safety: delete stray ENIs in this subnet (rare unless interface endpoints, etc.)
        enis = ec2.describe_network_interfaces(Filters=[{"Name": "subnet-id", "Values": [sn_id]}])["NetworkInterfaces"]
        for eni in enis:
            # Must be detached to delete; skip if in-use
            att = eni.get("Attachment")
            if att and att.get("Status") == "attached":
                # usually instances gone -> not expected here
                try_do(f"detach ENI {eni['NetworkInterfaceId']} (may fail if system-managed)",
                       ec2.detach_network_interface, AttachmentId=att["AttachmentId"], Force=True)
            try_do(f"delete ENI {eni['NetworkInterfaceId']}", ec2.delete_network_interface,
                   NetworkInterfaceId=eni["NetworkInterfaceId"])
        try_do(f"delete subnet {sn_id}", ec2.delete_subnet, SubnetId=sn_id)

    # --- 7) Delete non-default Security Groups -----------------------------------
    sgs = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
    for sg in sgs:
        if sg.get("GroupName") == "default":
            continue
        try_do(f"delete SG {sg['GroupId']}", ec2.delete_security_group, GroupId=sg["GroupId"])

    # --- 8) Delete the VPC --------------------------------------------------------
    try_do(f"delete VPC {vpc_id}", ec2.delete_vpc, VpcId=vpc_id)

    log("teardown complete.")


def parse_args():
    p = argparse.ArgumentParser(description="CS218 HW3 - boto3 automation")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("create", help="create the whole stack")
    pc.add_argument("--region", required=True)
    pc.add_argument("--prefix", required=True, help="name prefix for all resources (e.g., fang)")
    pc.add_argument("--key-name", required=True, help="existing EC2 key pair name")
    pc.add_argument("--ssh-cidr", default="0.0.0.0/0", help="SSH allowed CIDR for public SG")
    pc.set_defaults(func=create)

    pg = sub.add_parser("collect", help="export JSON files required by the homework")
    pg.add_argument("--region", required=True)
    pg.add_argument("--prefix", required=True)
    pg.set_defaults(func=collect)

    pt = sub.add_parser("teardown", help="destroy all resources created by this script")
    pt.add_argument("--region", required=True)
    pt.add_argument("--prefix", required=True)
    pt.set_defaults(func=teardown)
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    args.func(args)

