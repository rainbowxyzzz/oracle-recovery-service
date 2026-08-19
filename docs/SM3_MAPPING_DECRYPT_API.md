# SM3 映射反查接口

此接口用于通过 Doris 中的 SM3 映射表反查原始值。它不是算法层面的 SM3 解密，因为 SM3 是摘要算法，本身不可逆；接口只能在映射表中已经存在 `original_value -> sm3_value` 记录时返回原始值。

## 接口地址

`POST /api/v1/doris-sm3/decrypt`

## 鉴权

请求头必须携带系统 API Key：

```http
X-API-Key: change-me-before-production
Content-Type: application/json
```

## 请求体

```json
{
  "connection_id": "87718e55-3d3a-409a-943e-e14cc76e00d4",
  "field_category": "姓名",
  "field_mapping_database": "MAP",
  "field_mapping_table": "doris_mask_field_mappings",
  "items": [
    {
      "client_ref": "row-1",
      "encrypted_value": "a7b1..."
    },
    {
      "client_ref": "row-2",
      "encrypted_value": "f92c..."
    }
  ]
}
```

### 字段说明

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `connection_id` | 是 | 数据连接中配置的 Doris 连接 ID。 |
| `field_category` | 是 | 字段类别或字段名，例如 `姓名`、`手机号`、`证件号`、`地址`。接口会用它匹配字段关系表中的 `source_column_name`、`masked_column_name` 或 `mapping_table_name`。 |
| `items` | 否 | 批量查询项，适合调用方需要带自己的行号或业务 ID。 |
| `encrypted_values` | 否 | 简化批量格式，只传密文数组。`items` 和 `encrypted_values` 二选一。 |
| `field_aliases` | 否 | 额外字段别名，例如 `["name", "real_name"]`。 |
| `field_mapping_database` | 否 | 字段关系表所在库。不传时优先使用 `mapping_database`，再使用 Doris 连接默认库。 |
| `field_mapping_table` | 否 | 字段关系表名，默认 `doris_mask_field_mappings`。 |
| `mapping_database` | 否 | 直接指定映射表所在库，或作为字段关系表库的默认值。 |
| `mapping_table` | 否 | 直接指定映射表名。传了它以后接口不再查询字段关系表。 |
| `source_database` | 否 | 使用字段关系表时的筛选条件。 |
| `source_table` | 否 | 使用字段关系表时的筛选条件。 |
| `masked_database` | 否 | 使用字段关系表时的筛选条件。 |
| `masked_table` | 否 | 使用字段关系表时的筛选条件。 |

## 直接指定映射表

如果调用方已经知道某个字段使用的映射表，可以直接传 `mapping_database` 和 `mapping_table`：

```json
{
  "connection_id": "87718e55-3d3a-409a-943e-e14cc76e00d4",
  "field_category": "姓名",
  "mapping_database": "MAP",
  "mapping_table": "sm3_map_name",
  "encrypted_values": [
    "a7b1...",
    "f92c..."
  ]
}
```

## 返回示例

```json
{
  "field_category": "姓名",
  "total": 2,
  "found": 1,
  "not_found": 1,
  "ambiguous": 0,
  "mapping_sources": [
    {
      "mapping_database": "MAP",
      "mapping_table": "sm3_map_name",
      "original_column": "original_value",
      "encrypted_column": "sm3_value",
      "source_database": "csv_test",
      "source_table": "customer",
      "source_column": "name",
      "masked_database": "csv_test",
      "masked_table": "customer_sm3",
      "masked_column": "name",
      "updated_at": "2026-07-02 10:00:00"
    }
  ],
  "results": [
    {
      "index": 0,
      "encrypted_value": "a7b1...",
      "original_value": "张三",
      "found": true,
      "ambiguous": false,
      "client_ref": "row-1",
      "mapping_database": "MAP",
      "mapping_table": "sm3_map_name",
      "error": null
    },
    {
      "index": 1,
      "encrypted_value": "f92c...",
      "original_value": null,
      "found": false,
      "ambiguous": false,
      "client_ref": "row-2",
      "mapping_database": null,
      "mapping_table": null,
      "error": null
    }
  ],
  "warnings": [],
  "metadata": {
    "mode": "field_mapping_lookup",
    "algorithm": "SM3"
  }
}
```

## 结果语义

- `found=true`：映射表中找到唯一原始值。
- `found=false` 且 `ambiguous=false`：没有找到对应原始值。
- `ambiguous=true`：同一个密文在多个映射来源中查到不同原始值，接口不会强行返回某一个值。
- `mapping_sources`：本次实际使用的映射表来源。

## 限制

- 单次最多查询 1000 条密文。
- 只有已经写入 SM3 映射表的数据才能反查。
- 字段关系表默认使用 `doris_mask_field_mappings`，映射表默认字段必须是 `original_value` 和 `sm3_value`。
- `field_category` 会内置匹配常见别名，例如 `姓名/name/real_name`、`手机号/mobile/phone`、`证件号/id_card/cert_no`、`地址/address`。
