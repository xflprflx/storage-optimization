"""
Modelos de dados do Neokioski (SQLAlchemy 2.x, estilo declarativo tipado).

Relacionamentos:
    Branch  N <-> N  Warehouse                (tabela associativa branch_warehouse)
    StorageLocation  N -> 1 Branch (obrigatório)
    StorageLocation  N -> 1 Warehouse (opcional)
    Product          N -> 1 StorageLocation (default_location, opcional)
    StockItem        saldo físico de 1 Product em 1 StorageLocation
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, composite, mapped_column, relationship

from database import Base, DecimalType

# --------------------------------------------------------------------------- #
# Associação Many-to-Many Filial <-> Armazém
# --------------------------------------------------------------------------- #
branch_warehouse = Table(
    "branch_warehouse",
    Base.metadata,
    Column("branch_id", ForeignKey("branches.id", ondelete="CASCADE"), primary_key=True),
    Column("warehouse_id", ForeignKey("warehouses.id", ondelete="CASCADE"), primary_key=True),
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp()
    )


# --------------------------------------------------------------------------- #
# Filial
# --------------------------------------------------------------------------- #
class Branch(TimestampMixin, Base):
    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)

    warehouses: Mapped[list["Warehouse"]] = relationship(
        secondary=branch_warehouse, back_populates="branches", order_by="Warehouse.code"
    )
    locations: Mapped[list["StorageLocation"]] = relationship(back_populates="branch", passive_deletes=True)

    @property
    def label(self) -> str:
        return f"{self.code} - {self.name}"

    def __repr__(self) -> str:
        return f"<Branch {self.code}>"


# --------------------------------------------------------------------------- #
# Armazém / Depósito
# --------------------------------------------------------------------------- #
class Warehouse(TimestampMixin, Base):
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)

    branches: Mapped[list[Branch]] = relationship(
        secondary=branch_warehouse, back_populates="warehouses", order_by="Branch.code"
    )
    locations: Mapped[list["StorageLocation"]] = relationship(back_populates="warehouse", passive_deletes=True)

    @property
    def label(self) -> str:
        return f"{self.code} - {self.name}"

    def __repr__(self) -> str:
        return f"<Warehouse {self.code}>"


# --------------------------------------------------------------------------- #
# Endereço / Local de armazenamento
# --------------------------------------------------------------------------- #
class StorageLocation(TimestampMixin, Base):
    """Endereço físico. Hierarquia estrita: Aisle -> Bay -> Level -> Position -> Compartment.

    `compartment` vazio ("") representa uma posição GENÉRICA (sem compartimento).
    Usamos string vazia em vez de NULL para que o índice único funcione no SQLite
    (NULLs nunca colidem em índices únicos).
    """

    __tablename__ = "storage_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    warehouse_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    aisle: Mapped[str] = mapped_column(String(20), nullable=False)
    bay: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    position: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    compartment: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    address: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    is_occupied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    branch: Mapped[Branch] = relationship(back_populates="locations")
    warehouse: Mapped[Optional[Warehouse]] = relationship(back_populates="locations")
    stock_items: Mapped[list["StockItem"]] = relationship(
        back_populates="location", cascade="all, delete-orphan", passive_deletes=True
    )
    bins: Mapped[list["LocationBin"]] = relationship(
        back_populates="location", cascade="all, delete-orphan", passive_deletes=True,
        order_by="LocationBin.id",
    )

    @property
    def prefix(self) -> str:
        from services.addressing import build_address

        return build_address(self.aisle, self.bay, self.level, self.position, "")

    @property
    def scope_label(self) -> str:
        wh = self.warehouse.code if self.warehouse else "—"
        return f"{self.branch.code} / {wh}"

    @property
    def full_label(self) -> str:
        return f"{self.scope_label} / {self.address}"

    def __repr__(self) -> str:
        return f"<StorageLocation {self.address}>"


# Unicidade física: (filial, armazém, corredor, estante, nível, posição, compartimento).
# COALESCE(warehouse_id, 0) garante unicidade também para endereços sem armazém.
Index(
    "uq_storage_location_physical",
    StorageLocation.branch_id,
    func.coalesce(StorageLocation.warehouse_id, 0),
    StorageLocation.aisle,
    StorageLocation.bay,
    StorageLocation.level,
    StorageLocation.position,
    StorageLocation.compartment,
    unique=True,
)
Index("ix_storage_location_scope_address", StorageLocation.branch_id, StorageLocation.warehouse_id, StorageLocation.address)


# --------------------------------------------------------------------------- #
# Tipos de Bin e bins atribuídas a endereços
# --------------------------------------------------------------------------- #
class BinType(TimestampMixin, Base):
    """Tipo de bin/caixa/contentor: dimensões internas (cm) e capacidade (peso em kg, volume em litros)."""

    __tablename__ = "bin_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    height: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    width: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    length: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    max_weight: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)  # capacidade de peso (kg)
    # Capacidade de volume (litros). Se vazio, usa altura × largura × comprimento (cm) / 1000.
    max_volume: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    # Custo de compra de uma bin (R$). Compartimento sem custo próprio: rateio do custo da bin pai.
    cost: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    # Fração de uma bin inteira (compartimento): a bin pai é dividida numa grade de div_w colunas (na largura,
    # lado a lado) × div_l fileiras (no comprimento, da frente para o fundo); cada compartimento é uma célula.
    # Ex.: "1/2 P" = 2×1; 6 compartimentos = 2×3. As dimensões podem vir da bin pai (dims_auto) ou ser informadas.
    div_w: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    div_l: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bin_types.id", ondelete="RESTRICT"), nullable=True)
    fraction_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fraction_den: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    split_axis: Mapped[str] = mapped_column(String(10), nullable=False, default="width", server_default="width")
    dims_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    location_bins: Mapped[list["LocationBin"]] = relationship(back_populates="bin_type", passive_deletes=True)
    parent: Mapped[Optional["BinType"]] = relationship(remote_side="BinType.id", foreign_keys=[parent_id])

    @property
    def grid(self) -> tuple[int, int]:
        """(colunas na largura, fileiras no comprimento) da divisão da bin pai."""
        return (max(1, self.div_w or 1), max(1, self.div_l or 1))

    @property
    def is_fraction(self) -> bool:
        return self.parent_id is not None and self.grid[0] * self.grid[1] > 1

    @property
    def per_parent(self) -> int:
        """Quantos compartimentos deste tipo cabem numa bin pai (grade 2×3 -> 6)."""
        return self.grid[0] * self.grid[1] if self.is_fraction else 1

    @property
    def fraction(self) -> Optional[float]:
        return 1.0 / self.per_parent if self.is_fraction else None

    @property
    def fraction_label(self) -> str:
        return f"1/{self.per_parent}" if self.is_fraction else ""

    @property
    def grid_label(self) -> str:
        """Ex.: '2×3' (2 na largura × 3 no comprimento)."""
        return f"{self.grid[0]}×{self.grid[1]}" if self.is_fraction else ""

    @property
    def computed_volume(self) -> Optional[Decimal]:
        if self.height is None or self.width is None or self.length is None:
            return None
        return (self.height * self.width * self.length / Decimal(1000)).quantize(Decimal("0.0001"))

    @property
    def volume(self) -> Optional[Decimal]:
        """Capacidade de volume efetiva (litros): informada ou calculada pelas dimensões."""
        return self.max_volume if self.max_volume is not None else self.computed_volume

    @property
    def label(self) -> str:
        return f"{self.code} - {self.name}"


class ShelfLevel(TimestampMixin, Base):
    """Nível físico de uma estante (Corredor + Estante + Nível) dentro de um depósito.

    Existe independentemente dos endereços: um nível pode ter endereços próprios ou ser
    apenas um espaço RESERVADO para receber bins de endereços de outros níveis da mesma estante.
    """

    __tablename__ = "shelf_levels"

    id: Mapped[int] = mapped_column(primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)
    warehouse_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=True, index=True
    )
    aisle: Mapped[str] = mapped_column(String(20), nullable=False)
    bay: Mapped[str] = mapped_column(String(20), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    # Distância entre este nível e o de cima (altura útil do vão, cm). Em porta-paletes costuma ser
    # igual em toda a estrutura. Define a altura do nível no mapa e a altura máxima das bins nele.
    height_cm: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    notes: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    branch: Mapped[Branch] = relationship()
    warehouse: Mapped[Optional[Warehouse]] = relationship()
    capacities: Mapped[list["LevelCapacity"]] = relationship(
        back_populates="shelf_level", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def code(self) -> str:
        return f"{self.aisle}-{self.bay}-{self.level}"


Index(
    "uq_shelf_level_physical",
    ShelfLevel.branch_id,
    func.coalesce(ShelfLevel.warehouse_id, 0),
    ShelfLevel.aisle,
    ShelfLevel.bay,
    ShelfLevel.level,
    unique=True,
)


class LevelCapacity(TimestampMixin, Base):
    """Quantas bins de um tipo cabem no nível (ex.: 6× CX-P).

    Vários tipos no mesmo nível são ALTERNATIVAS: o uso do nível é a soma das frações
    Σ (bins do tipo ÷ capacidade do tipo); 100% = nível cheio.
    """

    __tablename__ = "level_capacities"
    __table_args__ = (UniqueConstraint("shelf_level_id", "bin_type_id", name="uq_level_capacity_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shelf_level_id: Mapped[int] = mapped_column(
        ForeignKey("shelf_levels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bin_type_id: Mapped[int] = mapped_column(ForeignKey("bin_types.id", ondelete="CASCADE"), nullable=False, index=True)
    max_bins: Mapped[int] = mapped_column(Integer, nullable=False)

    shelf_level: Mapped[ShelfLevel] = relationship(back_populates="capacities")
    bin_type: Mapped[BinType] = relationship()


class LocationBin(TimestampMixin, Base):
    """Bins de um tipo pertencentes a um endereço (ex.: 2× Caixa P).

    `shelf_level_id` indica ONDE as bins estão fisicamente:
      - NULL  -> no próprio nível do endereço (caso normal);
      - id    -> em outro nível da MESMA estante (nível extra, usado quando o material
                 do endereço não cabe no nível dele).
    O mesmo tipo pode aparecer em mais de uma linha, uma por nível.
    """

    __tablename__ = "location_bins"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bin_type_id: Mapped[int] = mapped_column(
        ForeignKey("bin_types.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    shelf_level_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("shelf_levels.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    location: Mapped["StorageLocation"] = relationship(back_populates="bins")
    bin_type: Mapped[BinType] = relationship(back_populates="location_bins")
    shelf_level: Mapped[Optional[ShelfLevel]] = relationship()


Index(
    "uq_location_bin_type_level",
    LocationBin.location_id,
    LocationBin.bin_type_id,
    func.coalesce(LocationBin.shelf_level_id, 0),
    unique=True,
)


# --------------------------------------------------------------------------- #
# Produto (com dimensões embutidas via composite)
# --------------------------------------------------------------------------- #
@dataclass
class Dimensions:
    height: Optional[Decimal]
    width: Optional[Decimal]
    length: Optional[Decimal]
    weight: Optional[Decimal]

    @property
    def volume(self) -> Optional[Decimal]:
        if self.height is None or self.width is None or self.length is None:
            return None
        return self.height * self.width * self.length

    @property
    def is_empty(self) -> bool:
        return all(v is None for v in (self.height, self.width, self.length, self.weight))


class Product(TimestampMixin, Base):
    """Cadastro ÚNICO do produto, compartilhado por todas as filiais: o que é do produto em si (SKU, nome, material,
    dimensões e peso). O que muda de filial para filial (endereço padrão, min e max level) fica na política de
    estoque (StockPolicy: produto × filial)."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(60), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Tipo de material (ex.: Eletrônico, Químico, Embalagem). Vazio = não informado.
    material_type: Mapped[str] = mapped_column(String(60), nullable=False, default="", server_default="", index=True)
    height: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    width: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    length: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    weight: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)

    dimensions: Mapped[Dimensions] = composite("height", "width", "length", "weight")

    policies: Mapped[list["StockPolicy"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", passive_deletes=True
    )
    stock_items: Mapped[list["StockItem"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<Product {self.sku}>"


class StockPolicy(TimestampMixin, Base):
    """Política de estoque do produto NA FILIAL: endereço padrão (onde o produto normalmente fica nessa filial) e
    níveis de estoque (min e max level, em unidades). Um registro por (filial, produto); o cadastro do produto é o
    mesmo para todas as filiais."""

    __tablename__ = "stock_policies"
    __table_args__ = (UniqueConstraint("branch_id", "product_id", name="uq_policy_branch_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    default_location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    min_level: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    max_level: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)

    product: Mapped["Product"] = relationship(back_populates="policies")
    branch: Mapped[Branch] = relationship()
    default_location: Mapped[Optional[StorageLocation]] = relationship()


# --------------------------------------------------------------------------- #
# Saldo físico
# --------------------------------------------------------------------------- #
class StockItem(TimestampMixin, Base):
    __tablename__ = "stock_items"
    __table_args__ = (UniqueConstraint("product_id", "location_id", name="uq_stock_product_location"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(DecimalType(4), nullable=False, default=Decimal("0"))

    product: Mapped[Product] = relationship(back_populates="stock_items")
    location: Mapped[StorageLocation] = relationship(back_populates="stock_items")


# --------------------------------------------------------------------------- #
# Conteúdo de cada bin física
# --------------------------------------------------------------------------- #
class BinContent(TimestampMixin, Base):
    """O que está dentro de cada bin física de um endereço.

    Uma bin física é identificada por (endereço, tipo, nº): a 2ª CX-M do endereço = (loc, CX-M, 2).
    A numeração segue a ordem das bins do endereço (primeiro as do nível do próprio endereço, depois as de
    níveis extras). O saldo do endereço continua em StockItem; aqui fica a divisão desse saldo pelas bins.
    `presumed` = distribuição calculada automaticamente pelo sistema (o usuário não informou);
    False = informada pelo usuário, pela importação ou por um plano aplicado.
    """

    __tablename__ = "bin_contents"
    __table_args__ = (UniqueConstraint("location_id", "bin_type_id", "seq", "product_id", name="uq_bin_content"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id", ondelete="CASCADE"), nullable=False, index=True)
    bin_type_id: Mapped[int] = mapped_column(ForeignKey("bin_types.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(DecimalType(4), nullable=False, default=Decimal("0"))
    presumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    product: Mapped["Product"] = relationship()
    bin_type: Mapped[BinType] = relationship()



# --------------------------------------------------------------------------- #
# Projeto de filial nova (greenfield): estruturas de armazenagem e projetos
# --------------------------------------------------------------------------- #
class StructureType(TimestampMixin, Base):
    """Estrutura de armazenagem modular (estante, porta-palete, flow rack...): um MÓDULO com frente,
    profundidade, altura total, nº de níveis e vão útil por nível (cm). Usada no projeto de filial nova."""

    __tablename__ = "structure_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="", server_default="")
    width: Mapped[Decimal] = mapped_column(DecimalType(4), nullable=False)  # frente do módulo (cm, útil)
    depth: Mapped[Decimal] = mapped_column(DecimalType(4), nullable=False)  # profundidade (cm)
    height: Mapped[Decimal] = mapped_column(DecimalType(4), nullable=False)  # altura total (cm)
    levels: Mapped[int] = mapped_column(Integer, nullable=False)
    level_height: Mapped[Decimal] = mapped_column(DecimalType(4), nullable=False)  # vão útil de cada nível (cm)
    max_load: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)  # carga máx. por nível (kg)
    cost: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)  # custo por módulo (R$)
    # zona de ouro (opcional): faixa de altura, a partir do piso, mais ergonômica para pegar itens (cm)
    golden_from: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    golden_to: Mapped[Optional[Decimal]] = mapped_column(DecimalType(4), nullable=True)
    notes: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")

    @property
    def label(self) -> str:
        return f"{self.code} - {self.name}"


class SiteProject(TimestampMixin, Base):
    """Projeto de uma filial nova: dimensões do imóvel, estruturas candidatas, demanda e parâmetros.
    Tudo fica em `config` (JSON) para o projeto poder evoluir sem migrações."""

    __tablename__ = "site_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class BayStructure(TimestampMixin, Base):
    """Estrutura de armazenagem de uma estante (Corredor + Estante) de um depósito existente: a estante é um
    módulo daquele tipo (frente, profundidade, nº de níveis e vão). Usada para preencher a capacidade e o vão
    dos níveis, desenhar o mapa e projetar filiais novas."""

    __tablename__ = "bay_structures"

    id: Mapped[int] = mapped_column(primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)
    warehouse_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=True, index=True
    )
    aisle: Mapped[str] = mapped_column(String(20), nullable=False)
    bay: Mapped[str] = mapped_column(String(20), nullable=False)
    structure_type_id: Mapped[int] = mapped_column(
        ForeignKey("structure_types.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    structure_type: Mapped[StructureType] = relationship()


Index(
    "uq_bay_structure_physical",
    BayStructure.branch_id,
    func.coalesce(BayStructure.warehouse_id, 0),
    BayStructure.aisle,
    BayStructure.bay,
    unique=True,
)


class PickHistory(TimestampMixin, Base):
    """Histórico de saídas de uma filial existente (resumo de uma planilha de pedidos ou da coluna Saída), usado na
    posição dos SKUs (slotting) dos modos Por endereço e Pool. Guarda só o resumo em `data` (JSON): linhas de pedido e
    unidades por SKU e os pares de SKUs que saem juntos (o arquivo bruto não fica no sistema)."""

    __tablename__ = "pick_histories"

    id: Mapped[int] = mapped_column(primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, unique=True,
                                           index=True)
    filename: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

