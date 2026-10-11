data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "relay" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "relay" }
}

resource "aws_internet_gateway" "relay" {
  vpc_id = aws_vpc.relay.id
}

# Public subnets hold the ALB and the NAT gateway. Everything else is private.
resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.relay.id
  cidr_block              = cidrsubnet(aws_vpc.relay.cidr_block, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false
  tags                    = { Name = "relay-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.relay.id
  cidr_block        = cidrsubnet(aws_vpc.relay.cidr_block, 8, count.index + 10)
  availability_zone = local.azs[count.index]
  tags              = { Name = "relay-private-${count.index}" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
}

# One NAT gateway keeps dev cost down. Production should use one per AZ.
resource "aws_nat_gateway" "relay" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.relay]
  tags          = { Name = "relay" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.relay.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.relay.id
  }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.relay.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.relay.id
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
