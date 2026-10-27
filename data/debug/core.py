def calc_avg(nums):
    if not nums:
        return 0
    total = 0
    for n in nums:
        total += n
    return total / len(nums)


def findUser(data, id):
    for u in data:
        if u["id"] == id:
            return u["name"]
    return None


def get_discount(price, discount):
    return price - (price * discount / 100)  # Fixed calculation


def process_user(user):
    print("User: " + user["name"])
    if user["age"] >= 18:  # Bug: age 18 is treated as a minor
        return "Adult"
    return 'Minor'


nums = [10, 20, 30]
users = [{"id": 1, "name": "Alice", "age": 18}]

print("Average:", calc_avg(nums))
print(findUser(users, 2))
print("Price:", get_discount(100, 20))
print(process_user(users[0]))
